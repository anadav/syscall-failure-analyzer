import os
import logging
from typing import Optional, Tuple, List, Set, Dict, Any, Union
from elftools.elf.elffile import ELFFile
from elftools.dwarf.dwarfinfo import DWARFInfo
from elftools.dwarf.ranges import BaseAddressEntry, RangeEntry
from elftools.dwarf.die import DIE
from elftools.dwarf.descriptions import describe_form_class
from elftools.common.exceptions import DWARFError
from elftools.dwarf.lineprogram import LineProgram
from elftools.dwarf.compileunit import CompileUnit

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class ELFDWARFAnalyzer:
    def __init__(self, filename: str):
        self.filename = filename
        self.elffile = None
        self.dwarfinfo = None
        self._aranges = None

    def __enter__(self):
        self.elffile = ELFFile(open(self.filename, 'rb'))
        self.dwarfinfo = self.elffile.get_dwarf_info()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._aranges = None
        if self.elffile:
            self.elffile.stream.close()

    def find_cu_by_address(self, address: int) -> Optional[Any]:
        """Find the Compilation Unit (CU) containing the given address."""
        if self._aranges is None:
            self._aranges = self.dwarfinfo.get_aranges()
        cu_offset = self._aranges.cu_offset_at_addr(address)
        if cu_offset is not None:
            return self.dwarfinfo.get_CU_at(cu_offset)
        return None

    def get_referenced_die(self, cu: Any, attr: Any) -> Optional[DIE]:
        """Get the DIE referenced by an attribute."""
        form_class = describe_form_class(attr.form)
        if form_class == 'reference':
            if attr.form.startswith('DW_FORM_ref'):
                return self.dwarfinfo.get_DIE_from_refaddr(cu.cu_offset + attr.value, cu)
            elif attr.form in ['DW_FORM_ref_addr', 'DW_FORM_data4', 'DW_FORM_data8']:
                return self.dwarfinfo.get_DIE_from_refaddr(attr.value)
        raise DWARFError(f"Unsupported reference form: {attr.form}")

    def resolve_type(self, die: DIE, prefix: str = "", is_type = False, depth: int = 0) -> str:
        """Resolve the type name of a DIE."""
        if depth > 10:
            return "<max depth reached>"

        if die.tag == 'DW_TAG_pointer_type':
            prefix = prefix + "*"

        if 'DW_AT_abstract_origin' in die.attributes:
            origin_attr = die.attributes['DW_AT_abstract_origin']
            origin_die = self.get_referenced_die(die.cu, origin_attr)
            return self.resolve_type(origin_die, prefix, is_type, depth + 1)

        if 'DW_AT_type' in die.attributes:
            type_attr = die.attributes['DW_AT_type']
            type_die = self.get_referenced_die(die.cu, type_attr)
            return self.resolve_type(type_die, prefix, True, depth + 1)

        if is_type and 'DW_AT_name' in die.attributes:
            return prefix + die.attributes['DW_AT_name'].value.decode('utf-8')

        return "void"
    
    def retrieve_name(self, die: DIE, depth: int = 0) -> str:
        """Resolve the type name of a DIE."""
        if depth > 10:
            return "<max depth reached>"

        if 'DW_AT_name' in die.attributes:
            return die.attributes['DW_AT_name'].value.decode('utf-8')

        if 'DW_AT_abstract_origin' in die.attributes:
            origin_attr = die.attributes['DW_AT_abstract_origin']
            origin_die = self.get_referenced_die(die.cu, origin_attr)
            return self.retrieve_name(origin_die, depth + 1)

        return "void"

    def find_function_return_type(self, cu, func_address: int) -> Optional[str]:
        """Find the return type of a function at a given address."""
        for die in cu.get_top_DIE().iter_children():
            if die.tag != 'DW_TAG_subprogram':
                continue

            if 'DW_AT_low_pc' not in die.attributes or 'DW_AT_high_pc' not in die.attributes:
                continue

            low_pc = die.attributes['DW_AT_low_pc'].value
            high_pc_attr = die.attributes['DW_AT_high_pc']
            high_pc = (low_pc + high_pc_attr.value 
                       if high_pc_attr.form != 'DW_FORM_addr' 
                       else high_pc_attr.value)

            if low_pc <= func_address < high_pc:
                logger.info(f"Found function DIE at offset: 0x{die.offset:x}")
                logger.info(f"Function address range: 0x{low_pc:x} - 0x{high_pc:x}")
                return self.resolve_type(die)

        logger.warning(f"No return type found for function at address {hex(func_address)}")
        return None

    @staticmethod
    def _get_ancestors(die: DIE) -> Set[DIE]:
        """Get the ancestors of a DIE."""
        ancestors = set()
        current_die = die
        while current_die:
            ancestors.add(current_die)
            current_die = current_die.get_parent()
        return ancestors

    def find_source_location(self, addr: int) -> List[Dict[str, Optional[Union[str, int]]]]:
        """Find the source file, line number, and column for a given function address, accounting for inlined code."""
        cu = self.find_cu_by_address(addr)
        if cu is None:
            logger.warning(f"No CU found for address {hex(addr)}")
            return []

        locations = []

        # Use line program to find the basic source location
        line_program = self.dwarfinfo.line_program_for_CU(cu)
        filename, line, column = None, None, None
        if line_program:
            filename, line, column = self.get_line_program_entry(line_program, addr)

        # Use DIEs to find inlined function information
        tags = {'DW_TAG_subprogram', 'DW_TAG_inlined_subroutine'}
        dies = self.find_dies_containing_address(cu, tags, addr)
        dies_with_ancestors = set()
        for die in dies:
            dies_with_ancestors.update(self._get_ancestors(die))
        dies = list(dies_with_ancestors)
        # find first subprogram
        subprogram_die = next((die for die in dies if die.tag == 'DW_TAG_subprogram'), None)
        subprogram_name = self.retrieve_name(subprogram_die) if subprogram_die else None

        inline_dies = [die for die in dies if die.tag == 'DW_TAG_inlined_subroutine']
        if len(inline_dies) > 0:
            inline_locations = [self.process_inlined_subroutine(die) for die in inline_dies]
            inline_locations.sort(key=lambda x: x[0], reverse=True)

            for _, call_file, call_line, call_column, callee_name in inline_locations:
                locations.append({'file': filename, 'line': line, 'col': column, 'func': callee_name})
                filename, line, column = call_file, call_line, call_column

        locations.append({'file': filename, 'line': line, 'col': column, 'func': subprogram_name})

        if not locations:
            logger.warning(f"No source location found for address {hex(addr)}")
        
        return locations

    def get_line_program_entry(self, line_program: LineProgram, addr: int) -> Optional[Tuple[Optional[str], Optional[int], Optional[int]]]:
        state = None
        prev_state = None

        # Iterate over entries to process the state machine
        for entry in line_program.get_entries():
            if not entry.state:
                continue  # Skip entries without a state

            if entry.state:
                current_state = entry.state

            # Handle the end sequence by invalidating the current state
            if entry.state and  entry.state.end_sequence:
                prev_state = None  # Invalidate state on end sequence
                continue  # Move to the next entry

            # Check if we have a valid previous state and if the address falls within the range
            if prev_state and prev_state.address <= addr < current_state.address:
                # The address falls between prev_state and current_state
                file_entry = line_program['file_entry'][prev_state.file]
                filename = self.get_full_path(line_program, file_entry)
                return (filename, prev_state.line, prev_state.column)

            # Update the previous state to the current state for the next iteration
            prev_state = current_state

        # If no match was found in the loop, handle the last state if applicable
        if prev_state and prev_state.address <= addr:
            file_entry = line_program['file_entry'][prev_state.file]
            filename = self.get_full_path(line_program, file_entry)
            return (filename, prev_state.line, prev_state.column)

        # If no valid entry is found
        return ('<unknown>', None, None)

    def find_dies_containing_address(self, cu, tags, addr:int):
        return [die for die in cu.iter_DIEs()
                if die.attributes and self.die_contains_address(die, cu, addr)]

    def process_subprogram(self, die, locations):
        file_name = self.get_die_source_file(die)
        line = die.attributes.get('DW_AT_decl_line', None)
        column = die.attributes.get('DW_AT_decl_column', None)
        func_name = self.retrieve_name(die)
        locations.append((file_name, line.value if line else None, column.value if column else None, func_name))

    def calculate_inline_depth(self, die: DIE) -> int:
        """
        Calculate the depth of an inlined function by traversing up the DIE tree.
        """
        depth = 0
        current_die = die
        while current_die:
            if current_die.tag == 'DW_TAG_inlined_subroutine':
                depth += 1
            current_die = current_die.get_parent()
        return depth

    def process_inlined_subroutine(self, die: DIE) -> Tuple[int, Optional[str], Optional[int], Optional[int], Optional[str]]:
        # Process the call site of the inlined subroutine
        call_file = self.get_die_source_file(die)
        call_line = die.attributes.get('DW_AT_call_line', None)
        call_line_value = call_line.value if call_line else None
        call_column = die.attributes.get('DW_AT_call_column', None)
        call_column_value = call_column.value if call_column else None
        func_name = self.retrieve_name(die)
        depth = self.calculate_inline_depth(die)
        return (depth, call_file, call_line_value, call_column_value, func_name)

    def process_abstract_origin(self, die, locations):
        file_name = self.get_die_source_file(die)
        line = die.attributes.get('DW_AT_decl_line', None)
        column = die.attributes.get('DW_AT_decl_column', None)
        locations.append((file_name, line.value if line else None, column.value if column else None))

    def get_die_source_file(self, die):
        file_attr = die.attributes.get('DW_AT_decl_file')
        if file_attr is None:
            if 'DW_AT_abstract_origin' in die.attributes:
                origin_attr = die.attributes['DW_AT_abstract_origin']
                origin_die = self.get_referenced_die(die.cu, origin_attr)
                return self.get_die_source_file(origin_die)
            return None
        file_index = file_attr.value
        line_program = self.dwarfinfo.line_program_for_CU(die.cu)
        file_entries = line_program['file_entry']
        if 0 < file_index <= len(file_entries):
            return self.get_full_path(line_program, file_entries[file_index - 1])
        return None