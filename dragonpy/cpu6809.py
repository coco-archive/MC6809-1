#!/usr/bin/env python
# coding: utf-8

"""
    DragonPy - Dragon 32 emulator in Python
    =======================================

    6809 is Big-Endian

    Links:
        http://dragondata.worldofdragon.org/Publications/inside-dragon.htm
        http://www.burgins.com/m6809.html
        http://koti.mbnet.fi/~atjs/mc6809/

    :copyleft: 2013-2014 by the DragonPy team, see AUTHORS for more details.
    :license: GNU GPL v3 or above, see LICENSE for more details.

    Based on:
        * ApplyPy by James Tauber (MIT license)
        * XRoar emulator by Ciaran Anscomb (GPL license)
    more info, see README
"""

import inspect
import logging
import os
import select
import socket
import sys
import time


import MC6809data.MC6809_data_raw2 as MC6809_data
from MC6809data.MC6809_data_raw2 import (
    OP_DATA, REG_A, REG_B, REG_CC, REG_D , REG_DP, REG_PC, REG_S, REG_U, REG_X, REG_Y
)
from dragonpy.components.memory import Memory
from dragonpy.cpu_utils.MC6809_registers import (
    ValueStorage8Bit, ConcatenatedAccumulator,
    ValueStorage16Bit, ConditionCodeRegister, cc_value2txt
, UndefinedRegister)
from dragonpy.utils.simple_debugger import print_exc_plus
from dragonpy.core.cpu_control_server import get_http_control_server
from dragonpy.cpu_utils.signed import signed8, signed16, signed5, unsigned8


log = logging.getLogger("DragonPy.cpu6809")
#HTML_TRACE = True
HTML_TRACE = False


def get_opdata():
    opdata = {}
    for instr_data in OP_DATA.values():
        for mnemonic, mnemonic_data in instr_data["mnemonic"].items():
            for op_code, op_data in mnemonic_data["ops"].items():
                op_data["mnemonic"] = mnemonic
                op_data["needs_ea"] = mnemonic_data["needs_ea"]
                for key in ("read_from_memory", "write_to_memory", "register"):
                    op_data[key] = mnemonic_data[key]
                opdata[op_code] = op_data
    return opdata

MC6809OP_DATA_DICT = get_opdata()


def activate_full_debug_logging():
    global log
    handler = logging.StreamHandler()
    handler.level = 5
    log.handlers = (handler,)
    log.critical("Activate full debug logging in %s!", __file__)





def byte2bit_string(data):
    return '{0:08b}'.format(data)


def hex_repr(d):
    txt = []
    for k, v in sorted(d.items()):
        if isinstance(v, int):
            txt.append("%s=$%x" % (k, v))
        else:
            txt.append("%s=%s" % (k, v))
    return " ".join(txt)


def opcode(*opcodes):
    """A decorator for opcodes"""
    def decorator(func):
        setattr(func, "_is_opcode", True)
        setattr(func, "_opcodes", opcodes)
        return func
    return decorator


class Instruction(object):
    def __init__(self, cpu, memory, opcode, opcode_data, instr_func):
        self.cpu = cpu
        self.memory = memory
        self.opcode = opcode
        self.data = opcode_data # dict entry from: MC6809OP_DATA_DICT
        self.instr_func = instr_func # unbound function for this op from cpu class

        self.static_kwargs = {
            "opcode": opcode
        }
        register_txt = opcode_data["register"]
        if register_txt is not None:
            self.static_kwargs["register"] = cpu.register_str2object[register_txt]

        self.get_ea_func = None
        self.get_m_func = None
        self.get_ea_m_func = None
        self.write_func = None

        self.OLD_EA = 0

#         print opcode_data
        addr_mode = opcode_data["addr_mode"]
        if addr_mode is not None and addr_mode != MC6809_data.INHERENT:
            needs_ea = opcode_data["needs_ea"]
            read_from_memory = opcode_data["read_from_memory"]

            if needs_ea and read_from_memory:
                ea_m_func_name = "get_ea_m_%s" % addr_mode.lower()
                self.get_ea_m_func = getattr(cpu, ea_m_func_name)
            else:
                if needs_ea:
                    # Instruction needs the ea (effective address)
                    ea_func_name = "get_ea_%s" % addr_mode.lower()
    #                 print ea_func_name
                    self.get_ea_func = getattr(cpu, ea_func_name)

                if read_from_memory is not None:
                    # Instruction read data from memory
                    m_func_name = "get_m_%s" % addr_mode.lower()
    #                 print m_func_name
                    self.get_m_func = getattr(cpu, m_func_name)

        _width2name = {MC6809_data.BYTE:"byte", MC6809_data.WORD:"word"}

        write_to_memory = opcode_data["write_to_memory"]
        if write_to_memory is not None:
            write_func_name = "write_%s" % (
                _width2name[write_to_memory]
            )
#             print write_func_name
            self.write_func = getattr(memory, write_func_name)
            assert needs_ea == True

#         log.debug("op code $%x data: %s" % (opcode, repr(instr_kwargs)))
#         log.debug(pprint.pformat(opcode_data))
#         log.debug(repr(opcode_data))
#         log.debug(pprint.pformat(instr_kwargs))
#         log.debug("-"*79)

    def __repr__(self):
        return "<Instruction $%x %s>" % (self.opcode, repr(self.data))

    def call_instr_func(self):
        self.op_kwargs = self.static_kwargs.copy()

        if self.get_ea_m_func is not None:
            self.op_kwargs["ea"], self.op_kwargs["m"] = self.get_ea_m_func()
        else:
            if self.get_ea_func is not None:
#                log.debug("\tget ea with %s", self.get_ea_func.__name__)
                self.op_kwargs["ea"] = self.get_ea_func()

            if self.get_m_func is not None:
#                log.debug("\tget m with %s", self.get_m_func.__name__)
                self.op_kwargs["m"] = self.get_m_func()

#         log.info("CPU cycles: %i", self.cpu.cycles)

        result = self.instr_func(**self.op_kwargs)
        self.cpu.cycles += self.data["cycles"]

        if self.write_func is not None:
            # Instruction write result to memory
            assert result is not None, (
                "Instruction %r write result to memory but returned None!"
            ) % self.instr_func.__name__
            ea, value = result
#            log.debug("\twrite with %r to $%x the value $%x",
#                self.write_func.__name__, ea, value
#            )
            self.write_func(ea, value)
        else:
            assert result is None, (
                "Instruction %r doesn't write result to memory but returned: %s!"
            ) % (self.instr_func.__name__, repr(result))


class IllegalInstruction(object):
    def __init__(self, cpu, opcode):
        self.cpu = cpu
        self.opcode = opcode

    def call_instr_func(self):
        op_address = self.cpu.last_op_address
        msg = "%x +++ Illegal op code: $%x" % (op_address, self.opcode)
        log.error(msg)
        raise RuntimeError(msg)


undefined_reg = UndefinedRegister()

class CPU(object):

    def __init__(self, cfg):
        self.cfg = cfg
        log.info("Use config: %s", cfg)

        if self.cfg.area_debug is not None:
            self.area_debug_active = False
            log.warn(
                "Activate area debug: Set debug level to %i from $%x to $%x" % self.cfg.area_debug
            )

        if self.cfg.area_debug_cycles is not None:
            log.critical("Activate debug after CPU cycle %i (%s)",
                self.cfg.area_debug_cycles, __file__
            )

        if self.cfg.max_cpu_cycles is not None:
            log.warn(
                "--max set: Stop CPU after %i cycles" % self.cfg.max_cpu_cycles
            )

        self.memory = Memory(self, cfg)

        # XXX: Maybe use multiprocessing to start a server?
        self.control_server = get_http_control_server(self, cfg)

        self.index_x = ValueStorage16Bit(REG_X, 0) # X - 16 bit index register
        self.index_y = ValueStorage16Bit(REG_Y, 0) # Y - 16 bit index register

        self.user_stack_pointer = ValueStorage16Bit(REG_U, 0) # U - 16 bit user-stack pointer
        self.user_stack_pointer.counter = 0

        # S - 16 bit system-stack pointer:
        # Position will be set by ROM code after detection of total installed RAM
        self._system_stack_pointer = ValueStorage16Bit(REG_S, 0)

        # PC - 16 bit program counter register
        self._program_counter = ValueStorage16Bit(REG_PC, self.cfg.RESET_VECTOR)

        self.accu_a = ValueStorage8Bit(REG_A, 0) # A - 8 bit accumulator
        self.accu_b = ValueStorage8Bit(REG_B, 0) # B - 8 bit accumulator

        # D - 16 bit concatenated reg. (A + B)
        self.accu_d = ConcatenatedAccumulator(REG_D, self.accu_a, self.accu_b)

        # DP - 8 bit direct page register
        self.direct_page = ValueStorage8Bit(REG_DP, 0)

        # 8 bit condition code register bits: E F H I N Z V C
        self.cc = ConditionCodeRegister()

        self.register_str2object = {
            REG_X: self.index_x,
            REG_Y: self.index_y,

            REG_U: self.user_stack_pointer,
            REG_S: self._system_stack_pointer,

            REG_PC: self._program_counter,

            REG_A: self.accu_a,
            REG_B: self.accu_b,
            REG_D: self.accu_d,

            REG_DP: self.direct_page,
            REG_CC: self.cc,

            undefined_reg.name: undefined_reg, # for TFR, EXG
        }

        self.cycles = 0
        self.last_op_address = 0 # Store the current run opcode memory address

#         log.debug("Add opcode functions:")
        self.opcode_dict = {}

        # Get the members not from class instance, so that's possible to
        # exclude properties without "activate" them.
        cls = type(self)
        for name, cls_method in inspect.getmembers(cls):
            if name.startswith("_") or isinstance(cls_method, property):
                continue

            try:
                opcodes = getattr(cls_method, "_opcodes")
            except AttributeError:
                continue

            instr_func = getattr(self, name)
            self._add_ops(opcodes, instr_func)

#         log.debug("illegal ops: %s" % ",".join(["$%x" % c for c in ILLEGAL_OPS]))
        # add illegal instruction
#         for opcode in ILLEGAL_OPS:
#             self.opcode_dict[opcode] = IllegalInstruction(self, opcode)

        self.running = True
        self.quit = False

        self.setup_trace_compare()

    def get_state(self):
        """
        used in unittests
        """
        state = {
            REG_X: self.index_x.get(),
            REG_Y: self.index_y.get(),

            REG_U: self.user_stack_pointer.get(),
            REG_S: self._system_stack_pointer.get(),

            REG_PC: self._program_counter.get(),

            REG_A: self.accu_a.get(),
            REG_B: self.accu_b.get(),

            REG_DP: self.direct_page.get(),
            REG_CC: self.cc.get(),

            "cycles": self.cycles,
            "RAM":self.memory.ram._mem[:], # copy of RAM
        }
        return state

    def set_state(self, state):
        """
        used in unittests
        """
        self.index_x.set(state[REG_X])
        self.index_y.set(state[REG_Y])

        self.user_stack_pointer.set(state[REG_U])
        self._system_stack_pointer.set(state[REG_S])

        self._program_counter.set(state[REG_PC])

        self.accu_a.set(state[REG_A])
        self.accu_b.set(state[REG_B])

        self.direct_page.set(state[REG_DP])
        self.cc.set(state[REG_CC])

        self.cycles = state["cycles"]
        self.memory.ram._mem = state["RAM"]

    def setup_trace_compare(self):
        self.xroar_trace_file = None
        self.v09_trace_file = None

        if not self.cfg.compare_trace:
            return

        if self.cfg.__class__.__name__ == "Dragon32Cfg":
            # XRoar bugtracking only with Dragon 32
            try:
                self.xroar_trace_file = open(os.path.expanduser(r"~/xroar_trace.txt"), "r")
            except IOError, err:
                log.error("No XRoar trace file: %s" % err)
                sys.exc_clear() # clears all information relating this exception
            else:
                self.xroar_trace_file.readline() # Skip reset line

        if self.cfg.__class__.__name__ == "SBC09Cfg":
            # v09 bugtrackung only with sbc09
            try:
                self.v09_trace_file = open(os.path.expanduser(r"~/v09_trace.txt"), "r")
            except IOError, err:
                log.error("No trace file: %s" % err)
                sys.exc_clear() # clears all information relating this exception

    ####

    def _add_ops(self, opcodes, instr_func):
#         log.debug("%20s: %s" % (
#             instr_func.__name__, ",".join(["$%x" % c for c in opcodes])
#         ))
        for opcode in opcodes:
            assert opcode not in self.opcode_dict, \
                "Opcode $%x (%s) defined more then one time!" % (
                    opcode, instr_func.__name__
            )

            try:
                opcode_data = MC6809OP_DATA_DICT[opcode]
            except KeyError:
                msg = "ERROR: no OP_DATA entry for $%x" % opcode
                log.error(msg)
                raise RuntimeError(msg)
                continue

            if HTML_TRACE:
                # FIXME: Add as CLI argument or complete remove?
                from dragonpy.cpu6809_html_debug import InstructionHTMLdebug
                InstructionClass = InstructionHTMLdebug
            else:
                InstructionClass = Instruction

            try:
                instruction = InstructionClass(self, self.memory, opcode, opcode_data, instr_func)
            except Exception, err:
                print >> sys.stderr, "Error init instruction for $%x" % opcode
                print >> sys.stderr, "opcode data: %s" % repr(opcode_data)
                print >> sys.stderr, "instr_func: %s" % instr_func.__name__
                raise

            self.opcode_dict[opcode] = instruction

    ####

    def reset(self):
        log.info("$%x CPU reset:" % self.program_counter)

        self.last_op_address = 0

        if self.cfg.__class__.__name__ == "SBC09Cfg":
            # first op is:
            # E400: 1AFF  reset  orcc #$FF  ;Disable interrupts.
#             log.debug("\tset CC register to 0xff")
#             self.cc.set(0xff)
            log.info("\tset CC register to 0x00")
            self.cc.set(0x00)
        else:
            log.info("\tset cc.F=1: FIRQ interrupt masked")
            self.cc.F = 1

            log.info("\tset cc.I=1: IRQ interrupt masked")
            self.cc.I = 1

#         log.debug("\tset PC to $%x" % self.cfg.RESET_VECTOR)
#         self.program_counter = self.cfg.RESET_VECTOR

        log.info("\tread word from $%x" % self.cfg.RESET_VECTOR)
        pc = self.memory.read_word(self.cfg.RESET_VECTOR)
        log.info("\tset PC to $%x" % (pc))
        self.program_counter = pc


    def get_and_call_next_op(self):
        op_address, opcode = self.read_pc_byte()
        self.call_instruction_func(op_address, opcode)

    same_op_count = 0
    last_op_code = None
    last_trace_line = None
    trace_line_no = 0
    def call_instruction_func(self, op_address, opcode):

#         if self.last_op_address == op_address:
#             # endless loop?
#             activate_full_debug_logging()
#             log.error("Endless loop???")

#         old_op_address = self.last_op_address
        self.last_op_address = op_address
        try:
            instruction = self.opcode_dict[opcode]
        except KeyError:
            msg = "$%x *** UNKNOWN OP $%x" % (op_address, opcode)
            log.error(msg)
            sys.exit(msg)

        try:
            instruction.call_instr_func()
#         if opcode not in (0x10, 0x11):
#         assert op_address != old_op_address, "$%x| Endless loop!" % opcode
        except Exception, err:
            # Display the op information log messages
            activate_full_debug_logging()
            log.info("Activate debug at $%x", op_address)

            # raise the error later, after op information log messages
            etype, evalue, etb = sys.exc_info()
            evalue = etype("%s\n   kwargs: %s" % (evalue, hex_repr(instruction.op_kwargs)))
        else:
            etype = None

        if log.level <= logging.INFO:
            kwargs_info = []
            if "register" in instruction.op_kwargs:
                kwargs_info.append(str(instruction.op_kwargs["register"]))
            if "ea" in instruction.op_kwargs:
                kwargs_info.append("ea:%04x" % instruction.op_kwargs["ea"])
            if "m" in instruction.op_kwargs:
                kwargs_info.append("m:%x" % instruction.op_kwargs["m"])


            mnemonic = instruction.data["mnemonic"]

            msg = "%(op_address)04x| %(opcode)-4s %(mnemonic)-6s %(kwargs)-27s %(cpu)s | %(cc)s | %(mem)s" % {
                "op_address": op_address,
                "opcode": "%02x" % opcode,
                "mnemonic": mnemonic,
                "kwargs": " ".join(kwargs_info),
                "cpu": self.get_info,
                "cc": self.cc.get_info,
                "mem": self.cfg.mem_info.get_shortest(op_address)
            }
#             if mnemonic in DEBUG_INSTR:
#                 if "ea" in self.op_kwargs:
#                     if self.OLD_EA != self.op_kwargs["ea"]:
#                         self.OLD_EA = self.op_kwargs["ea"]
#                         log.error(msg)
#                     else:
#                         log.info(msg)
#                 else:
#                     log.error(msg)
#             else:
            log.info(msg)

            if self.v09_trace_file is not None: # Hacked sbc09 bugtracking...
                if opcode in (0x10, 0x11): # PAGE 1/2 instructions
                    return

                if self.v09_trace_file is not None and self.last_trace_line is None:
                    self.last_trace_line = self.v09_trace_file.readline()
                    self.trace_line_no += 1
                ref_line = self.v09_trace_file.readline()
                self.trace_line_no += 1
                if ref_line == "":
                    log.error("no sbc09 log line (CPU cycles: %i)", self.cycles)
                    return

                log.info("sbc09: %s", ref_line)
                pc = int(self.last_trace_line[3:7], 16)
                if pc != op_address:
                    log.info("trace: %s" , ref_line)
                    log.info("own..: %s" , msg)
                    log.error("trace line number: %i - CPU cycles: %i", self.trace_line_no, self.cycles)
                    err_msg = "programm counter (own: $%x != sbc09: $%x) not the same as trace reference!\n" % (
                        op_address, pc
                    )
                    log.error(err_msg)
                    if etype is None:
                        raise RuntimeError(err_msg)

                ref_ab = ref_line[44:53] # e.g.: a=ff b=57
                own_ab = msg[52:61]
                if own_ab != ref_ab:
                    log.info("trace: %s" , ref_line)
                    log.info("own..: %s" , msg)
                    log.error("trace line number: %i - CPU cycles: %i", self.trace_line_no, self.cycles)
                    err_msg = "'a' or 'b' (own: %s != sbc09: %s) not the same as trace reference!\n" % (
                        own_ab, ref_ab
                    )
                    log.error(err_msg)
#                     if etype is None:
#                         raise RuntimeError(err_msg)

                ref_xy_us = ref_line[16:43] # e.g.: x=e5e4 y=0000 u=0400 s=03e6
                own_xy_us = msg[68:95]
                if own_xy_us != ref_xy_us:
                    log.info("trace: %s" , ref_line)
                    log.info("own..: %s" , msg)
                    log.error("trace line number: %i - CPU cycles: %i", self.trace_line_no, self.cycles)
                    err_msg = "x,y,u or s (own: %s != sbc09: %s) not the same as trace reference!\n" % (
                        own_xy_us, ref_xy_us
                    )
                    log.error(err_msg)
#                     if etype is None:
#                         raise RuntimeError(err_msg)

                cc1 = msg[98:106]
                ref_cc = int(ref_line[57:59], 16)
                ref_cc = cc_value2txt(ref_cc)
                if cc1 != ref_cc:
                    log.info("trace: %s" , ref_line)
                    log.info("own..: %s" , msg)
                    log.error("trace line number: %i - CPU cycles: %i", self.trace_line_no, self.cycles)
                    err_msg = "cc (own: %s != sbc09: %s) not the same as trace reference!\n" % (
                        cc1, ref_cc
                    )
                    log.error(err_msg)
#                     if etype is None:
#                         raise RuntimeError(err_msg)

                self.last_trace_line = ref_line


            if self.xroar_trace_file is not None: # Hacked XRoar bugtracking...
                if opcode in (0x10, 0x11): # PAGE 1/2 instructions
                    return

                ref_line = self.xroar_trace_file.readline()
                if ref_line == "":
                    log.error("no XRoar log line (CPU cycles: %i)", self.cycles)
                    return

                ref_line = ref_line.strip()

                # Add CC register info, e.g.: .F.IN..C
                xroar_cc = int(ref_line[49:51], 16)
                xroar_cc = cc_value2txt(xroar_cc)
                ref_line = "%s | %s" % (ref_line, xroar_cc)
#                 log.info("%s | %s", ref_line, xroar_cc)
                log.info("%s <<< XRoar", ref_line)

                registers1 = msg[52:95]
                registers2 = ref_line[52:95]
                if registers1 != registers2:
                    log.info("trace: %s" , ref_line)
                    log.info("own..: %s" , msg)
                    log.error("Error in CPU cycles: %i", self.cycles)
                    log.error("registers (own: %r != XRoar: %r) not the same as trace reference!\n" % (
                        registers1, registers2
                    ))

                cc1 = msg[98:106]
                if cc1 != xroar_cc:
                    log.info("trace: %s" , ref_line)
                    log.info("own..: %s" , msg)
                    log.error("Error in CPU cycles: %i", self.cycles)
                    err_msg = "CC (own: %r != XRoar: %r) not the same as trace reference!\n" % (
                        cc1, xroar_cc
                    )
                    log.error(err_msg)
                    if registers1 == registers2 and etype is None:
                        # same register values (except CC) but different CC
                        raise RuntimeError(err_msg)

                addr1 = msg.split("|", 1)[0]
                addr2 = ref_line.split("|", 1)[0]
                if addr1 != addr2:
                    log.info("trace: %s", ref_line)
                    log.info("own..: %s", msg)
                    log.error("%04x|Error in CPU cycles: %i", op_address, self.cycles)
                    log.error("address (own: %r != XRoar: %r) not the same as trace reference!\n" % (
                        addr1, addr2
                    ))

                mnemonic1 = msg[11:18].strip()
                mnemonic2 = ref_line[18:24].strip()
                if mnemonic1 != mnemonic2:
                    log.info("trace: %s", ref_line)
                    log.info("own..: %s" , msg)
                    log.error("%04x|Error in CPU cycles: %i", op_address, self.cycles)
                    err_msg = "mnemonic (own: %r != XRoar: %r) not the same as trace reference!\n" % (
                        mnemonic1, mnemonic2
                    )
                    log.error(err_msg)
                    if etype is None:
                        raise RuntimeError(err_msg)

                log.debug("\t%s", repr(instruction.data))

            log.debug("-"*79)

        if etype is not None:
            # raise the error while calling instruction, above.
            raise etype, evalue, etb


    @opcode(
        0x10, # PAGE 2 instructions
        0x11, # PAGE 3 instructions
    )
    def instruction_PAGE(self, opcode):
        """ call op from page 2 or 3 """
        op_address, opcode2 = self.read_pc_byte()
        paged_opcode = opcode * 256 + opcode2
#        log.debug("$%x *** call paged opcode $%x" % (
#            self.program_counter, paged_opcode
#        ))
        self.call_instruction_func(op_address - 1, paged_opcode)

    def run(self):
        while not self.quit:
            timeout = 0
            if not self.running:
                timeout = 1
            # Currently this handler blocks from the moment
            # a connection is accepted until the response
            # is sent. TODO: use an async HTTP server that
            # handles input data asynchronously.
            # XXX: use multiprocessing ?
            sockets = [self.control_server]
            rs, _, _ = select.select(sockets, [], [], timeout)
            for s in rs:
                if s is self.control_server:
                    self.control_server._handle_request_noblock()
                else:
                    pass

            for __ in xrange(self.cfg.BURST_COUNT):
                if not self.running:
                    break
                self.get_and_call_next_op()

            if self.cfg.max_cpu_cycles is not None \
                and self.cycles >= self.cfg.max_cpu_cycles:
                log.warn(
                    "Stop CPU after %i cycles" % self.cycles
                )
                self.quit = True
        log.critical("CPU quit")

    def test_run(self, start, end):
        log.warn("CPU test_run(): from $%x to $%x" % (start, end))
        self.program_counter = start
        log.debug("-"*79)
        while True:
            if self.program_counter == end:
                break
            self.get_and_call_next_op()

    def test_run2(self, start, count):
        log.warn("CPU test_run2(): from $%x count: %i" % (start, count))
        self.program_counter = start
        log.debug("-"*79)
        for __ in xrange(count):
            self.get_and_call_next_op()

    ####

    def get_CC(self):
        """ 8 bit condition code register bits: E F H I N Z V C """
        self.cc.status_as_byte()

    def get_X(self):
        """ return X - 16 bit index register """
        return self.index_x

    def get_Y(self):
        """ return Y - 16 bit index register """
        return self.index_y

    def get_U(self):
        """ return U - 16 bit user-stack pointer """
        return self.user_stack_pointer

    def get_S(self):
        """ return S - 16 bit system-stack pointer """
        return self._system_stack_pointer

    def get_V(self):
        """ return V - 16 bit variable inter-register """
        return self.value_register

    @property
    def get_info(self):
        return "cc=%02x a=%02x b=%02x dp=%02x x=%04x y=%04x u=%04x s=%04x" % (
            self.cc.get(),
            self.accu_a.get(), self.accu_b.get(),
            self.direct_page.get(),
            self.index_x.get(), self.index_y.get(),
            self.user_stack_pointer.get(), self.system_stack_pointer
        )

    ####

    def _get_system_stack_pointer(self):
        return self._system_stack_pointer.get()
    system_stack_pointer = property(_get_system_stack_pointer)

    def push_byte(self, stack_pointer, byte):
        """ pushed a byte onto stack """
        # FIXME: self._system_stack_pointer -= 1
        stack_pointer.decrement(1)
        addr = stack_pointer.get()

#        log.info(
#         log.error(
#            "%x|\tpush $%x to %s stack at $%x\t|%s",
#            self.last_op_address, byte, stack_pointer.name, addr,
#            self.cfg.mem_info.get_shortest(self.last_op_address)
#        )
        self.memory.write_byte(addr, byte)

    def pull_byte(self, stack_pointer):
        """ pulled a byte from stack """
        addr = stack_pointer.get()
        byte = self.memory.read_byte(addr)
#        log.info(
#         log.error(
#            "%x|\tpull $%x from %s stack at $%x\t|%s",
#            self.last_op_address, byte, stack_pointer.name, addr,
#            self.cfg.mem_info.get_shortest(self.last_op_address)
#        )

        # FIXME: self._system_stack_pointer += 1
        stack_pointer.increment(1)

        return byte

    def push_word(self, stack_pointer, word):
        # FIXME: self._system_stack_pointer -= 2
        stack_pointer.decrement(2)

        addr = stack_pointer.get()
#        log.info(
#         log.error(
#            "%x|\tpush word $%x to %s stack at $%x\t|%s",
#            self.last_op_address, word, stack_pointer.name, addr,
#            self.cfg.mem_info.get_shortest(self.last_op_address)
#        )

        self.memory.write_word(addr, word)

#         hi, lo = divmod(word, 0x100)
#         self.push_byte(hi)
#         self.push_byte(lo)

    def pull_word(self, stack_pointer):
        addr = stack_pointer.get()
        word = self.memory.read_word(addr)
#        log.info(
#         log.error(
#            "%x|\tpull word $%x from %s stack at $%x\t|%s",
#            self.last_op_address, word, stack_pointer.name, addr,
#            self.cfg.mem_info.get_shortest(self.last_op_address)
#        )
        # FIXME: self._system_stack_pointer += 2
        stack_pointer.increment(2)
        return word

    ####

    def _get_program_counter(self):
        return self._program_counter.get()

    def _set_program_counter(self, value):
        if self.cfg.area_debug_cycles is not None:
            if self.cycles >= self.cfg.area_debug_cycles:
                activate_full_debug_logging()
                log.debug("area debug activated after CPU cycle %i" % self.cycles)
                self.cfg.area_debug_cycles = None

        if self.cfg.area_debug is not None:
            # cfg.area_debug = (level, start, end)
            # TODO: make it workable with the other loggers
            if not self.area_debug_active:
                if value >= self.cfg.area_debug[1]: # start
                    self.area_debug_active = True
                    handler = logging.StreamHandler()
                    handler.level = self.cfg.area_debug[0] # FIXME: Doesn't work?!?!
                    log.handlers = (handler,)
#                     log.addHandler(handler)
                    log.info("Activate area debug at $%x", value)
            else:
                if value >= self.cfg.area_debug[2]: # end
                    log.info("Deactivate area debug at $%x", value)
                    self.area_debug_active = False
                    self.cfg.area_debug = None
#                     handler = log.handlers[-1]
#                     log.removeHandler(handler)
                    log.handlers = (logging.NullHandler(),)

        self._program_counter.set(value)
    program_counter = property(_get_program_counter, _set_program_counter)

    def read_pc_byte(self):
        op_addr = self.program_counter
        m = self.memory.read_byte(op_addr)
        self.program_counter += 1
#        log.log(5, "read pc byte: $%02x from $%04x", m, op_addr)
        return op_addr, m

    def read_pc_word(self):
        op_addr = self.program_counter
        m = self.memory.read_word(op_addr)
        self.program_counter += 2
#        log.log(5, "\tread pc word: $%04x from $%04x", m, op_addr)
        return op_addr, m

    ####

    def get_m_immediate(self):
        ea, m = self.read_pc_byte()
#        log.debug("\tget_m_immediate(): $%x from $%x", m, ea)
        return m

    def get_m_immediate_word(self):
        ea, m = self.read_pc_word()
#        log.debug("\tget_m_immediate_word(): $%x from $%x", m, ea)
        return m

    def get_ea_direct(self):
        op_addr, m = self.read_pc_byte()
        dp = self.direct_page.get()
        ea = dp << 8 | m
#        log.debug("\tget_ea_direct(): ea = dp << 8 | m  =>  $%x=$%x<<8|$%x", ea, dp, m)
        return ea

    def get_ea_m_direct(self):
        ea = self.get_ea_direct()
        m = self.memory.read_byte(ea)
#        log.debug("\tget_ea_m_direct(): ea=$%x m=$%x", ea, m)
        return ea, m

    def get_m_direct(self):
        ea = self.get_ea_direct()
        m = self.memory.read_byte(ea)
#        log.debug("\tget_m_direct(): $%x from $%x", m, ea)
        return m

    def get_m_direct_word(self):
        ea = self.get_ea_direct()
        m = self.memory.read_word(ea)
#        log.debug("\tget_m_direct(): $%x from $%x", m, ea)
        return m

    INDEX_POSTBYTE2STR = {
        0x00: REG_X, # 16 bit index register
        0x01: REG_Y, # 16 bit index register
        0x02: REG_U, # 16 bit user-stack pointer
        0x03: REG_S, # 16 bit system-stack pointer
    }
    def get_ea_indexed(self):
        """
        Calculate the address for all indexed addressing modes
        """
        addr, postbyte = self.read_pc_byte()
        log.debug("\tget_ea_indexed(): postbyte: $%02x (%s) from $%04x",
            postbyte, byte2bit_string(postbyte), addr
        )

        rr = (postbyte >> 5) & 3
        try:
            register_str = self.INDEX_POSTBYTE2STR[rr]
        except KeyError:
            raise RuntimeError("Register $%x doesn't exists! (postbyte: $%x)" % (rr, postbyte))

        register_obj = self.register_str2object[register_str]
        register_value = register_obj.get()
        log.debug("\t%02x == register %s: value $%x",
            rr, register_obj.name, register_value
        )

        if (postbyte & 0x80) == 0: # bit 7 == 0
            # EA = n, R - use 5-bit offset from post-byte
            offset = signed5(postbyte & 0x1f)
            ea = register_value + offset
            log.debug(
                "\tget_ea_indexed(): bit 7 == 0: reg.value: $%04x -> ea=$%04x + $%02x = $%04x",
                register_value, register_value, offset, ea
            )
            return ea

        addr_mode = postbyte & 0x0f
        self.cycles += 1
        offset = None
        # TODO: Optimized this, maybe use a dict mapping...
        if addr_mode == 0x0:
            log.debug("\t0000 0x0 | ,R+ | increment by 1")
            ea = register_value
            register_obj.increment(1)
        elif addr_mode == 0x1:
            log.debug("\t0001 0x1 | ,R++ | increment by 2")
            ea = register_value
            register_obj.increment(2)
            self.cycles += 1
        elif addr_mode == 0x2:
            log.debug("\t0010 0x2 | ,R- | decrement by 1")
            ea = register_obj.decrement(1)
        elif addr_mode == 0x3:
            log.debug("\t0011 0x3 | ,R-- | decrement by 2")
            ea = register_obj.decrement(2)
            self.cycles += 1
        elif addr_mode == 0x4:
            log.debug("\t0100 0x4 | ,R | No offset")
            ea = register_value
        elif addr_mode == 0x5:
            log.debug("\t0101 0x5 | B, R | B register offset")
            offset = signed8(self.accu_b.get())
        elif addr_mode == 0x6:
            log.debug("\t0110 0x6 | A, R | A register offset")
            offset = signed8(self.accu_a.get())
        elif addr_mode == 0x8:
            log.debug("\t1000 0x8 | n, R | 8 bit offset")
            offset = signed8(self.read_pc_byte()[1])
        elif addr_mode == 0x9:
            log.debug("\t1001 0x9 | n, R | 16 bit offset")
            offset = signed16(self.read_pc_word()[1])
            self.cycles += 1
        elif addr_mode == 0xa:
            log.debug("\t1010 0xa | illegal, set ea=0")
            ea = 0
        elif addr_mode == 0xb:
            log.debug("\t1011 0xb | D, R | D register offset")
            # D - 16 bit concatenated reg. (A + B)
            offset = signed16(self.accu_d.get()) # FIXME: signed16() ok?
            self.cycles += 1
        elif addr_mode == 0xc:
            log.debug("\t1100 0xc | n, PCR | 8 bit offset from program counter")
            __, value = self.read_pc_byte()
            value_signed = signed8(value)
            ea = self.program_counter + value_signed
            log.debug("\tea = pc($%x) + $%x = $%x (dez.: %i + %i = %i)",
                self.program_counter, value_signed, ea,
                self.program_counter, value_signed, ea,
            )
        elif addr_mode == 0xd:
            log.debug("\t1101 0xd | n, PCR | 16 bit offset from program counter")
            __, value = self.read_pc_word()
            value_signed = signed16(value)
            ea = self.program_counter + value_signed
            self.cycles += 1
            log.debug("\tea = pc($%x) + $%x = $%x (dez.: %i + %i = %i)",
                self.program_counter, value_signed, ea,
                self.program_counter, value_signed, ea,
            )
        elif addr_mode == 0xe:
            log.error("\tget_ea_indexed(): illegal address mode, use 0xffff")
            ea = 0xffff # illegal
        elif addr_mode == 0xf:
            log.debug("\t1111 0xf | [n] | 16 bit address - extended indirect")
            __, ea = self.read_pc_word()
        else:
            raise RuntimeError("Illegal indexed addressing mode: $%x" % addr_mode)

        if offset is not None:
            ea = register_value + offset
            log.debug("\t$%x + $%x = $%x (dez: %i + %i = %i)",
                register_value, offset, ea,
                register_value, offset, ea
            )

        ea = ea & 0xffff

        if postbyte & 0x10 != 0: # bit 4 is 1 -> Indirect
            # XXX: is that ok???
            log.debug("\tIndirect addressing: get new ea from $%x", ea)
            ea = self.memory.read_word(ea)
            log.debug("\tIndirect addressing: new ea is $%x", ea)

        log.debug("\tget_ea_indexed(): return ea=$%x", ea)
        return ea

    def get_m_indexed(self):
        ea = self.get_ea_indexed()
        m = self.memory.read_byte(ea)
        log.debug("\tget_m_indexed(): $%x from $%x", m, ea)
        return m

    def get_ea_m_indexed(self):
        ea = self.get_ea_indexed()
        m = self.memory.read_byte(ea)
        log.debug("\tget_ea_m_indexed(): ea = $%x m = $%x", ea, m)
        return ea, m

    def get_m_indexed_word(self):
        ea = self.get_ea_indexed()
        m = self.memory.read_word(ea)
        log.debug("\tget_m_indexed_word(): $%x from $%x", m, ea)
        return m

    def get_ea_extended(self):
        """
        extended indirect addressing mode takes a 2-byte value from post-bytes
        """
        attr, ea = self.read_pc_word()
#        log.debug("\tget_ea_extended() ea=$%x from $%x", ea, attr)
        return ea

    def get_m_extended(self):
        ea = self.get_ea_extended()
        m = self.memory.read_byte(ea)
#        log.debug("\tget_m_extended(): $%x from $%x", m, ea)
        return m

    def get_ea_m_extended(self):
        ea = self.get_ea_extended()
        m = self.memory.read_byte(ea)
#        log.debug("\tget_m_extended(): ea = $%x m = $%x", ea, m)
        return ea, m

    def get_m_extended_word(self):
        ea = self.get_ea_extended()
        m = self.memory.read_word(ea)
#        log.debug("\tget_m_extended_word(): $%x from $%x", m, ea)
        return m

    def get_ea_relative(self):
        addr, x = self.read_pc_byte()
        x = signed8(x)
        ea = self.program_counter + x
#        log.debug("\tget_ea_relative(): ea = $%x + %i = $%x \t| %s",
#            self.program_counter, x, ea,
#            self.cfg.mem_info.get_shortest(ea)
#        )
        return ea

    def get_ea_relative_word(self):
        addr, x = self.read_pc_word()
        ea = self.program_counter + x
#        log.debug("\tget_ea_relative_word(): ea = $%x + %i = $%x \t| %s",
#            self.program_counter, x, ea,
#            self.cfg.mem_info.get_shortest(ea)
#        )
        return ea

    def get_relative(self):
        ea = self.get_ea_relative()
        m = self.memory.read_byte(ea)
#        log.debug("\tget_relative(): ea = $%x m = $%x", ea, m)
        return ea, m

    def get_relative_word(self):
        ea = self.get_ea_relative()
        m = self.memory.read_word(ea)
#        log.debug("\tget_relative_word(): ea = $%x m = $%x", ea, m)
        return ea, m

    #### Op methods:

    @opcode(# Add B accumulator to X (unsigned)
        0x3a, # ABX (inherent)
    )
    def instruction_ABX(self, opcode):
        """
        Add the 8-bit unsigned value in accumulator B into index register X.

        source code forms: ABX

        CC bits "HNZVC": -----
        """
        old = self.index_x.get()
        b = self.accu_b.get()
        new = self.index_x.increment(b)
#        log.debug("%x %02x ABX: X($%x) += B($%x) = $%x" % (
#            self.program_counter, opcode,
#            old, b, new
#        ))

    @opcode(# Add memory to accumulator with carry
        0x89, 0x99, 0xa9, 0xb9, # ADCA (immediate, direct, indexed, extended)
        0xc9, 0xd9, 0xe9, 0xf9, # ADCB (immediate, direct, indexed, extended)
    )
    def instruction_ADC(self, opcode, m, register):
        """
        Adds the contents of the C (carry) bit and the memory byte into an 8-bit
        accumulator.

        source code forms: ADCA P; ADCB P

        CC bits "HNZVC": aaaaa
        """
        a = register.get()
        r = a + m + self.cc.C
        register.set(r)
#        log.debug("$%x %02x ADC %s: %i + %i + %i = %i (=$%x)" % (
#            self.program_counter, opcode, register.name,
#            a, m, self.cc.C, r, r
#        ))
        self.cc.clear_HNZVC()
        self.cc.update_HNZVC_8(a, m, r)

    @opcode(# Add memory to D accumulator
        0xc3, 0xd3, 0xe3, 0xf3, # ADDD (immediate, direct, indexed, extended)
    )
    def instruction_ADD16(self, opcode, m, register):
        """
        Adds the 16-bit memory value into the 16-bit accumulator

        source code forms: ADDD P

        CC bits "HNZVC": -aaaa
        """
        assert register.WIDTH == 16
        old = register.get()
        r = old + m
        register.set(r)
#        log.debug("$%x %02x %02x ADD16 %s: $%02x + $%02x = $%02x" % (
#            self.program_counter, opcode, m,
#            register.name,
#            old, m, r
#        ))
        self.cc.clear_NZVC()
        self.cc.update_NZVC_16(old, m, r)

    @opcode(# Add memory to accumulator
        0x8b, 0x9b, 0xab, 0xbb, # ADDA (immediate, direct, indexed, extended)
        0xcb, 0xdb, 0xeb, 0xfb, # ADDB (immediate, direct, indexed, extended)
    )
    def instruction_ADD8(self, opcode, m, register):
        """
        Adds the memory byte into an 8-bit accumulator.

        source code forms: ADDA P; ADDB P

        CC bits "HNZVC": aaaaa
        """
        assert register.WIDTH == 8
        old = register.get()
        r = old + m
        register.set(r)
#         log.debug("$%x %02x %02x ADD8 %s: $%02x + $%02x = $%02x" % (
#             self.program_counter, opcode, m,
#             register.name,
#             old, m, r
#         ))
        self.cc.clear_HNZVC()
        self.cc.update_HNZVC_8(old, m, r)

    @opcode(0xf, 0x6f, 0x7f) # CLR (direct, indexed, extended)
    def instruction_CLR_memory(self, opcode, ea):
        """
        Clear memory location
        source code forms: CLR
        CC bits "HNZVC": -0100
        """
        self.cc.update_0100()
        return ea, 0x00

    @opcode(0x4f, 0x5f) # CLRA / CLRB (inherent)
    def instruction_CLR_register(self, opcode, register):
        """
        Clear accumulator A or B

        source code forms: CLRA; CLRB
        CC bits "HNZVC": -0100
        """
        register.set(0x00)
        self.cc.update_0100()

    def COM(self, value):
        """
        CC bits "HNZVC": -aa01
        """
        # value = unsigned8(~value) # the bits of m inverted
        value = ~value # the bits of m inverted
        self.cc.clear_NZ()
        self.cc.update_NZ01_8(value)
        return value

    @opcode(# Complement memory location
        0x3, 0x63, 0x73, # COM (direct, indexed, extended)
    )
    def instruction_COM_memory(self, opcode, ea, m):
        """
        Replaces the contents of memory location M with its logical complement.
        source code forms: COM Q
        """
        r = self.COM(value=m)
#        log.debug("$%x COM memory $%x to $%x" % (
#            self.program_counter, m, r,
#        ))
        return ea, r & 0xff

    @opcode(# Complement accumulator
        0x43, # COMA (inherent)
        0x53, # COMB (inherent)
    )
    def instruction_COM_register(self, opcode, register):
        """
        Replaces the contents of accumulator A or B with its logical complement.
        source code forms: COMA; COMB
        """
        register.set(self.COM(value=register.get()))
#        log.debug("$%x COM %s" % (
#            self.program_counter, register.name,
#        ))

    @opcode(# Decimal adjust A accumulator
        0x19, # DAA (inherent)
    )
    def instruction_DAA(self, opcode):
        """
        The sequence of a single-byte add instruction on accumulator A (either
        ADDA or ADCA) and a following decimal addition adjust instruction
        results in a BCD addition with an appropriate carry bit. Both values to
        be added must be in proper BCD form (each nibble such that: 0 <= nibble
        <= 9). Multiple-precision addition must add the carry generated by this
        decimal addition adjust into the next higher digit during the add
        operation (ADCA) immediately prior to the next decimal addition adjust.

        source code forms: DAA

        CC bits "HNZVC": -aa0a
        """
        tmp = 0
        a = self.accu_a.get()
        cc = self.cc.get()

        if (a & 0x0f) >= 0x0a or cc & self.cc.H:
            tmp |= 0x06

        if a >= 0x90 and (a & 0x0f) >= 0x0a:
            tmp |= 0x60

        if a >= 0xa0 or cc & self.cc.C:
            tmp |= 0x60

        new_value = tmp + a
        self.accu_a.set(new_value)

        # CC.C NOT cleared, only set if appropriate
        self.cc.clear_NZV()
        self.cc.update_NZC_8(tmp)

    def DEC(self, a):
        """
        Subtract one from the register. The carry bit is not affected, thus
        allowing this instruction to be used as a loop counter in multiple-
        precision computations. When operating on unsigned values, only BEQ and
        BNE branches can be expected to behave consistently. When operating on
        twos complement values, all signed branches are available.

        source code forms: DEC Q; DECA; DECB

        CC bits "HNZVC": -aaa-
        """
        r = a - 1
        self.cc.clear_NZV()
        self.cc.update_NZ_8(r)
        if r == 0x7f:
            self.cc.V = 1
        return r

    @opcode(0xa, 0x6a, 0x7a) # DEC (direct, indexed, extended)
    def instruction_DEC_memory(self, opcode, ea, m):
        """ Decrement memory location """
        r = self.DEC(m)
#        log.debug("$%x DEC memory value $%x -1 = $%x and write it to $%x \t| %s" % (
#            self.program_counter,
#            m, r, ea,
#            self.cfg.mem_info.get_shortest(ea)
#        ))
        return ea, r & 0xff

    @opcode(0x4a, 0x5a) # DECA / DECB (inherent)
    def instruction_DEC_register(self, opcode, register):
        """ Decrement accumulator """
        a = register.get()
        r = self.DEC(a)
#        log.debug("$%x DEC %s value $%x -1 = $%x" % (
#            self.program_counter,
#            register.name, a, r
#        ))
        register.set(r)

    def INC(self, a):
        r = a + 1
        self.cc.clear_NZV()
        self.cc.update_NZ_8(r)
        if r == 0x80:
            self.cc.V = 1
        return r

    @opcode(# Increment accumulator
        0x4c, # INCA (inherent)
        0x5c, # INCB (inherent)
    )
    def instruction_INC_register(self, opcode, register):
        """
        Adds to the register. The carry bit is not affected, thus allowing this
        instruction to be used as a loop counter in multiple-precision
        computations. When operating on unsigned values, only the BEQ and BNE
        branches can be expected to behave consistently. When operating on twos
        complement values, all signed branches are correctly available.

        source code forms: INC Q; INCA; INCB

        CC bits "HNZVC": -aaa-
        """
        a = register.get()
        r = self.INC(a)
        r = register.set(r)

    @opcode(# Increment memory location
        0xc, 0x6c, 0x7c, # INC (direct, indexed, extended)
    )
    def instruction_INC_memory(self, opcode, ea, m):
        """
        Adds to the register. The carry bit is not affected, thus allowing this
        instruction to be used as a loop counter in multiple-precision
        computations. When operating on unsigned values, only the BEQ and BNE
        branches can be expected to behave consistently. When operating on twos
        complement values, all signed branches are correctly available.

        source code forms: INC Q; INCA; INCB

        CC bits "HNZVC": -aaa-
        """
        r = self.INC(m)
        return ea, r & 0xff

    @opcode(# Load effective address into an indexable register
        0x32, # LEAS (indexed)
        0x33, # LEAU (indexed)
    )
    def instruction_LEA_pointer(self, opcode, ea, register):
        """
        Calculates the effective address from the indexed addressing mode and
        places the address in an indexable register.

        LEAU and LEAS do not affect the Z bit to allow cleaning up the stack
        while returning the Z bit as a parameter to a calling routine, and also
        for MC6800 INS/DES compatibility.

        LEAU -10,U   U-10 -> U     Subtracts 10 from U
        LEAS -10,S   S-10 -> S     Used to reserve area on stack
        LEAS 10,S    S+10 -> S     Used to 'clean up' stack
        LEAX 5,S     S+5 -> X      Transfers as well as adds

        source code forms: LEAS, LEAU

        CC bits "HNZVC": -----
        """
#         log.debug(
#             "$%04x LEA %s: Set %s to $%04x \t| %s" % (
#             self.program_counter,
#             register.name, register.name, ea,
#             self.cfg.mem_info.get_shortest(ea)
#         ))
        register.set(ea)

    @opcode(# Load effective address into an indexable register
        0x30, # LEAX (indexed)
        0x31, # LEAY (indexed)
    )
    def instruction_LEA_register(self, opcode, ea, register):
        """ see instruction_LEA_pointer

        LEAX and LEAY affect the Z (zero) bit to allow use of these registers
        as counters and for MC6800 INX/DEX compatibility.

        LEAX 10,X    X+10 -> X     Adds 5-bit constant 10 to X
        LEAX 500,X   X+500 -> X    Adds 16-bit constant 500 to X
        LEAY A,Y     Y+A -> Y      Adds 8-bit accumulator to Y
        LEAY D,Y     Y+D -> Y      Adds 16-bit D accumulator to Y

        source code forms: LEAX, LEAY

        CC bits "HNZVC": --a--
        """
#         log.debug("$%04x LEA %s: Set %s to $%04x \t| %s" % (
#             self.program_counter,
#             register.name, register.name, ea,
#             self.cfg.mem_info.get_shortest(ea)
#         ))
        register.set(ea)
        self.cc.Z = 0
        self.cc.set_Z16(ea)

    @opcode(# Unsigned multiply (A * B ? D)
        0x3d, # MUL (inherent)
    )
    def instruction_MUL(self, opcode):
        """
        Multiply the unsigned binary numbers in the accumulators and place the
        result in both accumulators (ACCA contains the most-significant byte of
        the result). Unsigned multiply allows multiple-precision operations.

        The C (carry) bit allows rounding the most-significant byte through the
        sequence: MUL, ADCA #0.

        source code forms: MUL

        CC bits "HNZVC": --a-a
        """
        r = self.accu_a.get() * self.accu_b.get()
        self.accu_d.set(r)
        self.cc.Z = 1 if r == 0 else 0
        self.cc.C = 1 if r & 0x80 else 0

    @opcode(# Negate accumulator
        0x40, # NEGA (inherent)
        0x50, # NEGB (inherent)
    )
    def instruction_NEG_register(self, opcode, register):
        """
        Replaces the register with its twos complement. The C (carry) bit
        represents a borrow and is set to the inverse of the resulting binary
        carry. Note that 80 16 is replaced by itself and only in this case is
        the V (overflow) bit set. The value 00 16 is also replaced by itself,
        and only in this case is the C (carry) bit cleared.

        source code forms: NEG Q; NEGA; NEG B

        CC bits "HNZVC": uaaaa
        """
        x = register.get()
        r = x * -1 # same as: r = ~x + 1
        register.set(r)
#        log.debug("$%04x NEG %s $%02x to $%02x" % (
#            self.program_counter, register.name, x, r,
#        ))
        self.cc.clear_NZVC()
        self.cc.update_NZVC_8(0, x, r)

    _wrong_NEG = 0
    @opcode(0x0, 0x60, 0x70) # NEG (direct, indexed, extended)
    def instruction_NEG_memory(self, opcode, ea, m):
        """ Negate memory """
        if opcode == 0x0 and ea == 0x0 and m == 0x0:
            self._wrong_NEG += 1
            if self._wrong_NEG > 10:
                raise RuntimeError, "Wrong PC ???"
        else:
            self._wrong_NEG = 0

        r = m * -1 # same as: r = ~m + 1
#         r = r & 0xff # XXX: 0xff here?

        log.debug("$%04x NEG $%02x from %04x to $%02x" % (
            self.program_counter, m, ea, r,
        ))
        self.cc.clear_NZVC()
        self.cc.update_NZVC_8(0, m, r)
        return ea, r & 0xff

    @opcode(0x12) # NOP (inherent)
    def instruction_NOP(self, opcode):
        """
        No operation

        source code forms: NOP

        CC bits "HNZVC": -----
        """
#        log.debug("\tNOP")



    @opcode(# Push A, B, CC, DP, D, X, Y, U, or PC onto stack
        0x36, # PSHU (immediate)
        0x34, # PSHS (immediate)
    )
    def instruction_PSH(self, opcode, m, register):
        """
        All, some, or none of the processor registers are pushed onto stack
        (with the exception of stack pointer itself).

        A single register may be placed on the stack with the condition codes
        set by doing an autodecrement store onto the stack (example: STX ,--S).

        source code forms: b7 b6 b5 b4 b3 b2 b1 b0 PC U Y X DP B A CC push order
        ->

        CC bits "HNZVC": -----
        """
        assert register in (self._system_stack_pointer, self.user_stack_pointer)

        def push(register_str, stack_pointer):
            register_obj = self.register_str2object[register_str]
            data = register_obj.get()

            log.debug("\tpush %s with data $%x", register_obj.name, data)

            if register_obj.WIDTH == 8:
                self.push_byte(register, data)
            else:
                assert register_obj.WIDTH == 16
                self.push_word(register, data)

        log.debug("$%x PSH%s post byte: $%x", self.program_counter, register.name, m)

        # m = postbyte
        if m & 0x80: push(REG_PC, register) # 16 bit program counter register
        if m & 0x40: push(REG_U, register) #  16 bit user-stack pointer
        if m & 0x20: push(REG_Y, register) #  16 bit index register
        if m & 0x10: push(REG_X, register) #  16 bit index register
        if m & 0x08: push(REG_DP, register) #  8 bit direct page register
        if m & 0x04: push(REG_B, register) #   8 bit accumulator
        if m & 0x02: push(REG_A, register) #   8 bit accumulator
        if m & 0x01: push(REG_CC, register) #  8 bit condition code register


    @opcode(# Pull A, B, CC, DP, D, X, Y, U, or PC from stack
        0x37, # PULU (immediate)
        0x35, # PULS (immediate)
    )
    def instruction_PUL(self, opcode, m, register):
        """
        All, some, or none of the processor registers are pulled from stack
        (with the exception of stack pointer itself).

        A single register may be pulled from the stack with condition codes set
        by doing an autoincrement load from the stack (example: LDX ,S++).

        source code forms: b7 b6 b5 b4 b3 b2 b1 b0 PC U Y X DP B A CC = pull
        order

        CC bits "HNZVC": ccccc
        """
        assert register in (self._system_stack_pointer, self.user_stack_pointer)

        def pull(register_str, stack_pointer):
            reg_obj = self.register_str2object[register_str]

            reg_width = reg_obj.WIDTH # 8 / 16
            if reg_width == 8:
                data = self.pull_byte(stack_pointer)
            else:
                assert reg_width == 16
                data = self.pull_word(stack_pointer)

            reg_obj.set(data)

#        log.debug("$%x PUL%s:", self.program_counter, register.name)

        # m = postbyte
        if m & 0x01: pull(REG_CC, register) # 8 bit condition code register
        if m & 0x02: pull(REG_A, register) # 8 bit accumulator
        if m & 0x04: pull(REG_B, register) # 8 bit accumulator
        if m & 0x08: pull(REG_DP, register) # 8 bit direct page register
        if m & 0x10: pull(REG_X, register) # 16 bit index register
        if m & 0x20: pull(REG_Y, register) # 16 bit index register
        if m & 0x40: pull(REG_U, register) # 16 bit user-stack pointer
        if m & 0x80: pull(REG_PC, register) # 16 bit program counter register

    @opcode(# Subtract memory from accumulator with borrow
        0x82, 0x92, 0xa2, 0xb2, # SBCA (immediate, direct, indexed, extended)
        0xc2, 0xd2, 0xe2, 0xf2, # SBCB (immediate, direct, indexed, extended)
    )
    def instruction_SBC(self, opcode, m, register):
        """
        Subtracts the contents of memory location M and the borrow (in the C
        (carry) bit) from the contents of the designated 8-bit register, and
        places the result in that register. The C bit represents a borrow and is
        set to the inverse of the resulting binary carry.

        source code forms: SBCA P; SBCB P

        CC bits "HNZVC": uaaaa
        """
        a = register.get()
        r = a - m - self.cc.C
        register.set(r)
#        log.debug("$%x %02x SBC %s: %i - %i - %i = %i (=$%x)" % (
#            self.program_counter, opcode, register.name,
#            a, m, self.cc.C, r, r
#        ))
        self.cc.clear_NZVC()
        self.cc.update_NZVC_8(a, m, r)

    @opcode(# Sign Extend B accumulator into A accumulator
        0x1d, # SEX (inherent)
    )
    def instruction_SEX(self, opcode):
        """
        This instruction transforms a twos complement 8-bit value in accumulator
        B into a twos complement 16-bit value in the D accumulator.

        source code forms: SEX

        CC bits "HNZVC": -aa0-

            // 0x1d SEX inherent
            case 0x1d:
                WREG_A = (RREG_B & 0x80) ? 0xff : 0;
                CLR_NZ;
                SET_NZ16(REG_D);
                peek_byte(cpu, REG_PC);

        #define SIGNED(b) ((Word)(b&0x80?b|0xff00:b))
        case 0x1D: /* SEX */ tw=SIGNED(ibreg); SETNZ16(tw) SETDREG(tw) break;
        """
        b = self.accu_b.get()
        if b & 0x80 == 0:
            self.accu_a.set(0x00)

        d = self.accu_d.get()

#        log.debug("SEX: b=$%x ; $%x&0x80=$%x ; d=$%x", b, b, (b & 0x80), d)

        self.cc.clear_NZ()
        self.cc.update_NZ_16(d)



    @opcode(# Subtract memory from accumulator
        0x80, 0x90, 0xa0, 0xb0, # SUBA (immediate, direct, indexed, extended)
        0xc0, 0xd0, 0xe0, 0xf0, # SUBB (immediate, direct, indexed, extended)
        0x83, 0x93, 0xa3, 0xb3, # SUBD (immediate, direct, indexed, extended)
    )
    def instruction_SUB(self, opcode, m, register):
        """
        Subtracts the value in memory location M from the contents of a
        register. The C (carry) bit represents a borrow and is set to the
        inverse of the resulting binary carry.

        source code forms: SUBA P; SUBB P; SUBD P

        CC bits "HNZVC": uaaaa
        """
        r = register.get()
        r_new = r - m
        register.set(r_new)
#        log.debug("$%x SUB8 %s: $%x - $%x = $%x (dez.: %i - %i = %i)" % (
#            self.program_counter, register.name,
#            r, m, r_new,
#            r, m, r_new,
#        ))
        self.cc.clear_NZVC()
        if register.WIDTH == 8:
            self.cc.update_NZVC_8(r, m, r_new)
        else:
            assert register.WIDTH == 16
            self.cc.update_NZVC_16(r, m, r_new)


    # ---- Register Changes - FIXME: Better name for this section?!? ----

    REGISTER_BIT2STR = {
        0x0: REG_D, # 0000 - 16 bit concatenated reg.(A B)
        0x1: REG_X, # 0001 - 16 bit index register
        0x2: REG_Y, # 0010 - 16 bit index register
        0x3: REG_U, # 0011 - 16 bit user-stack pointer
        0x4: REG_S, # 0100 - 16 bit system-stack pointer
        0x5: REG_PC, # 0101 - 16 bit program counter register
        0x6: undefined_reg.name, # undefined
        0x7: undefined_reg.name, # undefined
        0x8: REG_A, # 1000 - 8 bit accumulator
        0x9: REG_B, # 1001 - 8 bit accumulator
        0xa: REG_CC, # 1010 - 8 bit condition code register as flags
        0xb: REG_DP, # 1011 - 8 bit direct page register
        0xc: undefined_reg.name, # undefined
        0xd: undefined_reg.name, # undefined
        0xe: undefined_reg.name, # undefined
        0xf: undefined_reg.name, # undefined
    }

    def _get_register_obj(self, addr):
        addr_str = self.REGISTER_BIT2STR[addr]
        reg_obj = self.register_str2object[addr_str]
#         log.debug("get register obj: addr: $%x addr_str: %s -> register: %s" % (
#             addr, addr_str, reg_obj.name
#         ))
#         log.debug(repr(self.register_str2object))
        return reg_obj

    def _get_register_and_value(self, addr):
        reg = self._get_register_obj(addr)
        reg_value = reg.get()
        return reg, reg_value

    def _convert_differend_width(self, src_reg, src_value, dst_reg):
        """
        e.g.:
         8bit   $cd TFR into 16bit, results in: $cd00
        16bit $1234 TFR into  8bit, results in:   $34

        TODO: verify this behaviour on real hardware
        see: http://archive.worldofdragon.org/phpBB3/viewtopic.php?f=8&t=4886
        """
        if src_reg.WIDTH == 8 and dst_reg.WIDTH == 16:
            # e.g.: $cd -> $ffcd
            src_value += 0xff00
        elif src_reg.WIDTH == 16 and dst_reg.WIDTH == 8:
            # e.g.: $1234 -> $34
            src_value = src_value | 0xff00
        return src_value

    @opcode(0x1f) # TFR (immediate)
    def instruction_TFR(self, opcode, m):
        """
        source code forms: TFR R1, R2
        CC bits "HNZVC": ccccc
        """
        high, low = divmod(m, 16)
        src_reg, src_value = self._get_register_and_value(high)
        dst_reg = self._get_register_obj(low)
        src_value = self._convert_differend_width(src_reg, src_value, dst_reg)
        dst_reg.set(src_value)
#         log.debug("\tTFR: Set %s to $%x from %s",
#             dst_reg, src_value, src_reg.name
#         )

    @opcode(# Exchange R1 with R2
        0x1e, # EXG (immediate)
    )
    def instruction_EXG(self, opcode, m):
        """
        source code forms: EXG R1,R2
        CC bits "HNZVC": ccccc
        """
        high, low = divmod(m, 0x10)
        reg1, reg1_value = self._get_register_and_value(high)
        reg2, reg2_value = self._get_register_and_value(low)

        new_reg1_value = self._convert_differend_width(reg2, reg2_value, reg1)
        new_reg2_value = self._convert_differend_width(reg1, reg1_value, reg2)

        reg1.set(new_reg1_value)
        reg2.set(new_reg2_value)

#         log.debug("\tEXG: %s($%x) <-> %s($%x)",
#             reg1.name, reg1_value, reg2.name, reg2_value
#         )

    # ---- Store / Load ----


    @opcode(# Load register from memory
        0xcc, 0xdc, 0xec, 0xfc, # LDD (immediate, direct, indexed, extended)
        0x10ce, 0x10de, 0x10ee, 0x10fe, # LDS (immediate, direct, indexed, extended)
        0xce, 0xde, 0xee, 0xfe, # LDU (immediate, direct, indexed, extended)
        0x8e, 0x9e, 0xae, 0xbe, # LDX (immediate, direct, indexed, extended)
        0x108e, 0x109e, 0x10ae, 0x10be, # LDY (immediate, direct, indexed, extended)
    )
    def instruction_LD16(self, opcode, m, register):
        """
        Load the contents of the memory location M:M+1 into the designated
        16-bit register.

        source code forms: LDD P; LDX P; LDY P; LDS P; LDU P

        CC bits "HNZVC": -aa0-
        """
#        log.debug("$%x LD16 set %s to $%x \t| %s" % (
#            self.program_counter,
#            register.name, m,
#            self.cfg.mem_info.get_shortest(m)
#        ))
        register.set(m)
        self.cc.clear_NZV()
        self.cc.update_NZ_16(m)

    @opcode(# Load accumulator from memory
        0x86, 0x96, 0xa6, 0xb6, # LDA (immediate, direct, indexed, extended)
        0xc6, 0xd6, 0xe6, 0xf6, # LDB (immediate, direct, indexed, extended)
    )
    def instruction_LD8(self, opcode, m, register):
        """
        Loads the contents of memory location M into the designated register.

        source code forms: LDA P; LDB P

        CC bits "HNZVC": -aa0-
        """
#        log.debug("$%x LD8 %s = $%x" % (
#            self.program_counter,
#            register.name, m,
#        ))
        register.set(m)
        self.cc.clear_NZV()
        self.cc.update_NZ_8(m)

    @opcode(# Store register to memory
        0xdd, 0xed, 0xfd, # STD (direct, indexed, extended)
        0x10df, 0x10ef, 0x10ff, # STS (direct, indexed, extended)
        0xdf, 0xef, 0xff, # STU (direct, indexed, extended)
        0x9f, 0xaf, 0xbf, # STX (direct, indexed, extended)
        0x109f, 0x10af, 0x10bf, # STY (direct, indexed, extended)
    )
    def instruction_ST16(self, opcode, ea, register):
        """
        Writes the contents of a 16-bit register into two consecutive memory
        locations.

        source code forms: STD P; STX P; STY P; STS P; STU P

        CC bits "HNZVC": -aa0-
        """
        value = register.get()
        log.debug("$%x ST16 store value $%x from %s at $%x \t| %s" % (
            self.program_counter,
            value, register.name, ea,
            self.cfg.mem_info.get_shortest(ea)
        ))
        self.cc.clear_NZV()
        self.cc.update_NZ_16(value)
        return ea, value # write word to Memory

    @opcode(# Store accumulator to memory
        0x97, 0xa7, 0xb7, # STA (direct, indexed, extended)
        0xd7, 0xe7, 0xf7, # STB (direct, indexed, extended)
    )
    def instruction_ST8(self, opcode, ea, register):
        """
        Writes the contents of an 8-bit register into a memory location.

        source code forms: STA P; STB P

        CC bits "HNZVC": -aa0-
        """
        value = register.get()
        log.debug("$%x ST8 store value $%x from %s at $%x \t| %s" % (
            self.program_counter,
            value, register.name, ea,
            self.cfg.mem_info.get_shortest(ea)
        ))
        self.cc.clear_NZV()
        self.cc.update_NZ_8(value)
        return ea, value # write byte to Memory


    # ---- Logical Operations ----


    @opcode(# AND memory with accumulator
        0x84, 0x94, 0xa4, 0xb4, # ANDA (immediate, direct, indexed, extended)
        0xc4, 0xd4, 0xe4, 0xf4, # ANDB (immediate, direct, indexed, extended)
    )
    def instruction_AND(self, opcode, m, register):
        """
        Performs the logical AND operation between the contents of an
        accumulator and the contents of memory location M and the result is
        stored in the accumulator.

        source code forms: ANDA P; ANDB P

        CC bits "HNZVC": -aa0-
        """
        a = register.get()
        r = a & m
        register.set(r)
        self.cc.clear_NZV()
        self.cc.update_NZ_8(r)
#        log.debug("\tAND %s: %i & %i = %i",
#            register.name, a, m, r
#        )

    @opcode(# Exclusive OR memory with accumulator
        0x88, 0x98, 0xa8, 0xb8, # EORA (immediate, direct, indexed, extended)
        0xc8, 0xd8, 0xe8, 0xf8, # EORB (immediate, direct, indexed, extended)
    )
    def instruction_EOR(self, opcode, m, register):
        """
        The contents of memory location M is exclusive ORed into an 8-bit
        register.

        source code forms: EORA P; EORB P

        CC bits "HNZVC": -aa0-
        """
        a = register.get()
        r = a ^ m
        register.set(r)
        self.cc.clear_NZV()
        self.cc.update_NZ_8(r)
#        log.debug("\tEOR %s: %i ^ %i = %i",
#            register.name, a, m, r
#        )

    @opcode(# OR memory with accumulator
        0x8a, 0x9a, 0xaa, 0xba, # ORA (immediate, direct, indexed, extended)
        0xca, 0xda, 0xea, 0xfa, # ORB (immediate, direct, indexed, extended)
    )
    def instruction_OR(self, opcode, m, register):
        """
        Performs an inclusive OR operation between the contents of accumulator A
        or B and the contents of memory location M and the result is stored in
        accumulator A or B.

        source code forms: ORA P; ORB P

        CC bits "HNZVC": -aa0-
        """
        a = register.get()
        r = a | m
        register.set(r)
        self.cc.clear_NZV()
        self.cc.update_NZ_8(r)
#         log.debug("$%04x OR %s: %02x | %02x = %02x",
#             self.program_counter, register.name, a, m, r
#         )


    # ---- CC manipulation ----


    @opcode(# AND condition code register
        0x1c, # ANDCC (immediate)
    )
    def instruction_ANDCC(self, opcode, m, register):
        """
        Performs a logical AND between the condition code register and the
        immediate byte specified in the instruction and places the result in the
        condition code register.

        source code forms: ANDCC #xx

        CC bits "HNZVC": ddddd
        """
        assert register == self.cc

        old_cc = self.cc.get()
        new_cc = old_cc & m
        self.cc.set(new_cc)
        log.debug("\tANDCC: $%x AND $%x = $%x | set CC to %s",
            old_cc, m, new_cc, self.cc.get_info
        )

    @opcode(# OR condition code register
        0x1a, # ORCC (immediate)
    )
    def instruction_ORCC(self, opcode, m, register):
        """
        Performs an inclusive OR operation between the contents of the condition
        code registers and the immediate value, and the result is placed in the
        condition code register. This instruction may be used to set interrupt
        masks (disable interrupts) or any other bit(s).

        source code forms: ORCC #XX

        CC bits "HNZVC": ddddd
        """
        assert register == self.cc

        old_cc = self.cc.get()
        new_cc = old_cc | m
        self.cc.set(new_cc)
        log.debug("\tORCC: $%x OR $%x = $%x | set CC to %s",
            old_cc, m, new_cc, self.cc.get_info
        )

    # ---- Test Instructions ----


    @opcode(# Compare memory from stack pointer
        0x1083, 0x1093, 0x10a3, 0x10b3, # CMPD (immediate, direct, indexed, extended)
        0x118c, 0x119c, 0x11ac, 0x11bc, # CMPS (immediate, direct, indexed, extended)
        0x1183, 0x1193, 0x11a3, 0x11b3, # CMPU (immediate, direct, indexed, extended)
        0x8c, 0x9c, 0xac, 0xbc, # CMPX (immediate, direct, indexed, extended)
        0x108c, 0x109c, 0x10ac, 0x10bc, # CMPY (immediate, direct, indexed, extended)
    )
    def instruction_CMP16(self, opcode, m, register):
        """
        Compares the 16-bit contents of the concatenated memory locations M:M+1
        to the contents of the specified register and sets the appropriate
        condition codes. Neither the memory locations nor the specified register
        is modified unless autoincrement or autodecrement are used. The carry
        flag represents a borrow and is set to the inverse of the resulting
        binary carry.

        source code forms: CMPD P; CMPX P; CMPY P; CMPU P; CMPS P

        CC bits "HNZVC": -aaaa
        """
        r = register.get()
        r_new = r - m
        log.warn("$%x CMP16 %s $%x - $%x = $%x" % (
            self.program_counter,
            register.name,
            r, m, r_new,
        ))
        self.cc.clear_NZVC()
        self.cc.update_NZVC_16(r, m, r_new)

    @opcode(# Compare memory from accumulator
        0x81, 0x91, 0xa1, 0xb1, # CMPA (immediate, direct, indexed, extended)
        0xc1, 0xd1, 0xe1, 0xf1, # CMPB (immediate, direct, indexed, extended)
    )
    def instruction_CMP8(self, opcode, m, register):
        """
        Compares the contents of memory location to the contents of the
        specified register and sets the appropriate condition codes. Neither
        memory location M nor the specified register is modified. The carry flag
        represents a borrow and is set to the inverse of the resulting binary
        carry.

        source code forms: CMPA P; CMPB P

        CC bits "HNZVC": uaaaa
        """
        r = register.get()
        r_new = r - m
#         log.warn("$%x CMP8 %s $%x - $%x = $%x" % (
#             self.program_counter,
#             register.name,
#             r, m, r_new,
#         ))
        self.cc.clear_NZVC()
        self.cc.update_NZVC_8(r, m, r_new)


    @opcode(# Bit test memory with accumulator
        0x85, 0x95, 0xa5, 0xb5, # BITA (immediate, direct, indexed, extended)
        0xc5, 0xd5, 0xe5, 0xf5, # BITB (immediate, direct, indexed, extended)
    )
    def instruction_BIT(self, opcode, m, register):
        """
        Performs the logical AND of the contents of accumulator A or B and the
        contents of memory location M and modifies the condition codes
        accordingly. The contents of accumulator A or B and memory location M
        are not affected.

        source code forms: BITA P; BITB P

        CC bits "HNZVC": -aa0-
        """
        x = register.get()
        r = m & x
#        log.debug("$%x BIT update CC with $%x (m:%i & %s:%i)" % (
#            self.program_counter,
#            r, m, register.name, x
#        ))
        self.cc.clear_NZV()
        self.cc.update_NZ_8(r)

    @opcode(# Test accumulator
        0x4d, # TSTA (inherent)
        0x5d, # TSTB (inherent)
    )
    def instruction_TST_register(self, opcode, register):
        """
        Set the N (negative) and Z (zero) bits according to the contents of
        accumulator A or B, and clear the V (overflow) bit. The TST instruction
        provides only minimum information when testing unsigned values; since no
        unsigned value is less than zero, BLO and BLS have no utility. While BHI
        could be used after TST, it provides exactly the same control as BNE,
        which is preferred. The signed branches are available.

        The MC6800 processor clears the C (carry) bit.

        source code forms: TST Q; TSTA; TSTB

        CC bits "HNZVC": -aa0-
        """
        x = register.get()
        self.cc.clear_NZV()
        self.cc.update_NZ_8(x)

    @opcode(0xd, 0x6d, 0x7d) # TST (direct, indexed, extended)
    def instruction_TST_memory(self, opcode, m):
        """ Test memory location """
#         log.debug("$%x TST m=$%02x" % (
#             self.program_counter, m
#         ))
        self.cc.clear_NZV()
        self.cc.update_NZ_8(m)

    # ---- Programm Flow Instructions ----


    @opcode(# Jump
        0xe, 0x6e, 0x7e, # JMP (direct, indexed, extended)
    )
    def instruction_JMP(self, opcode, ea):
        """
        Program control is transferred to the effective address.

        source code forms: JMP EA

        CC bits "HNZVC": -----
        """
#        log.info("%x|\tJMP to $%x \t| %s" % (
#            self.last_op_address,
#            ea, self.cfg.mem_info.get_shortest(ea)
#        ))
        self.program_counter = ea

    @opcode(# Return from subroutine
        0x39, # RTS (inherent)
    )
    def instruction_RTS(self, opcode):
        """
        Program control is returned from the subroutine to the calling program.
        The return address is pulled from the stack.

        source code forms: RTS

        CC bits "HNZVC": -----
        """
        ea = self.pull_word(self._system_stack_pointer)
#        log.info("%x|\tRTS to $%x \t| %s" % (
#            self.last_op_address,
#            ea,
#            self.cfg.mem_info.get_shortest(ea)
#        ))
        self.program_counter = ea

    @opcode(
        # Branch to subroutine:
        0x8d, # BSR (relative)
        0x17, # LBSR (relative)
        # Jump to subroutine:
        0x9d, 0xad, 0xbd, # JSR (direct, indexed, extended)
    )
    def instruction_BSR_JSR(self, opcode, ea):
        """
        Program control is transferred to the effective address after storing
        the return address on the hardware stack.

        A return from subroutine (RTS) instruction is used to reverse this
        process and must be the last instruction executed in a subroutine.

        source code forms: BSR dd; LBSR DDDD; JSR EA

        CC bits "HNZVC": -----
        """
#        log.info("%x|\tJSR/BSR to $%x \t| %s" % (
#            self.last_op_address,
#            ea, self.cfg.mem_info.get_shortest(ea)
#        ))
        self.push_word(self._system_stack_pointer, self.program_counter)
        self.program_counter = ea


    # ---- Branch Instructions ----


    @opcode(# Branch if equal
        0x27, # BEQ (relative)
        0x1027, # LBEQ (relative)
    )
    def instruction_BEQ(self, opcode, ea):
        """
        Tests the state of the Z (zero) bit and causes a branch if it is set.
        When used after a subtract or compare operation, this instruction will
        branch if the compared values, signed or unsigned, were exactly the
        same.

        source code forms: BEQ dd; LBEQ DDDD

        CC bits "HNZVC": -----
        """
        if self.cc.Z == 1:
#            log.info("$%x BEQ branch to $%x, because Z==1 \t| %s" % (
#                self.program_counter, ea, self.cfg.mem_info.get_shortest(ea)
#            ))
            self.program_counter = ea
#        else:
#            log.debug("$%x BEQ: don't branch to $%x, because Z==0 \t| %s" % (
#                self.program_counter, ea, self.cfg.mem_info.get_shortest(ea)
#            ))

    @opcode(# Branch if greater than or equal (signed)
        0x2c, # BGE (relative)
        0x102c, # LBGE (relative)
    )
    def instruction_BGE(self, opcode, ea):
        """
        Causes a branch if the N (negative) bit and the V (overflow) bit are
        either both set or both clear. That is, branch if the sign of a valid
        twos complement result is, or would be, positive. When used after a
        subtract or compare operation on twos complement values, this
        instruction will branch if the register was greater than or equal to the
        memory register.

        source code forms: BGE dd; LBGE DDDD

        CC bits "HNZVC": -----
        """
        # Note these variantes are the same:
        #    self.cc.N == self.cc.V
        #    (self.cc.N ^ self.cc.V) == 0
        #    not operator.xor(self.cc.N, self.cc.V)
        if self.cc.N == self.cc.V:
#            log.info("$%x BGE branch to $%x, because N XOR V == 0 \t| %s" % (
#                self.program_counter, ea, self.cfg.mem_info.get_shortest(ea)
#            ))
            self.program_counter = ea
#         else:
#             log.debug("$%x BGE: don't branch to $%x, because N XOR V != 0 \t| %s" % (
#                 self.program_counter, ea, self.cfg.mem_info.get_shortest(ea)
#             ))

    @opcode(# Branch if greater (signed)
        0x2e, # BGT (relative)
        0x102e, # LBGT (relative)
    )
    def instruction_BGT(self, opcode, ea):
        """
        Causes a branch if the N (negative) bit and V (overflow) bit are either
        both set or both clear and the Z (zero) bit is clear. In other words,
        branch if the sign of a valid twos complement result is, or would be,
        positive and not zero. When used after a subtract or compare operation
        on twos complement values, this instruction will branch if the register
        was greater than the memory register.

        source code forms: BGT dd; LBGT DDDD

        CC bits "HNZVC": -----
        """
        # Note these variantes are the same:
        #    not ((self.cc.N ^ self.cc.V) == 1 or self.cc.Z == 1)
        #    not ((self.cc.N ^ self.cc.V) | self.cc.Z)
        #    self.cc.N == self.cc.V and self.cc.Z == 0
        # ;)
        if not self.cc.Z and self.cc.N == self.cc.V:
#            log.info("$%x BGT branch to $%x, because (N==V and Z==0) \t| %s" % (
#                self.program_counter, ea, self.cfg.mem_info.get_shortest(ea)
#            ))
            self.program_counter = ea
#         else:
#            log.debug("$%x BGT: don't branch to $%x, because (N==V and Z==0) is False \t| %s" % (
#                self.program_counter, ea, self.cfg.mem_info.get_shortest(ea)
#            ))

    @opcode(# Branch if higher (unsigned)
        0x22, # BHI (relative)
        0x1022, # LBHI (relative)
    )
    def instruction_BHI(self, opcode, ea):
        """
        Causes a branch if the previous operation caused neither a carry nor a
        zero result. When used after a subtract or compare operation on unsigned
        binary values, this instruction will branch if the register was higher
        than the memory register.

        Generally not useful after INC/DEC, LD/TST, and TST/CLR/COM
        instructions.

        source code forms: BHI dd; LBHI DDDD

        CC bits "HNZVC": -----
        """
        if self.cc.C == 0 and self.cc.Z == 0:
#            log.info("$%x BHI branch to $%x, because C==0 and Z==0 \t| %s" % (
#                self.program_counter, ea, self.cfg.mem_info.get_shortest(ea)
#            ))
            self.program_counter = ea
#         else:
#            log.debug("$%x BHI: don't branch to $%x, because C and Z not 0 \t| %s" % (
#                self.program_counter, ea, self.cfg.mem_info.get_shortest(ea)
#            ))

    @opcode(# Branch if less than or equal (signed)
        0x2f, # BLE (relative)
        0x102f, # LBLE (relative)
    )
    def instruction_BLE(self, opcode, ea):
        """
        Causes a branch if the exclusive OR of the N (negative) and V (overflow)
        bits is 1 or if the Z (zero) bit is set. That is, branch if the sign of
        a valid twos complement result is, or would be, negative. When used
        after a subtract or compare operation on twos complement values, this
        instruction will branch if the register was less than or equal to the
        memory register.

        source code forms: BLE dd; LBLE DDDD

        CC bits "HNZVC": -----
        """
        if (self.cc.N ^ self.cc.V) == 1 or self.cc.Z == 1:
#            log.info("$%x BLE branch to $%x, because N^V==1 or Z==1 \t| %s" % (
#                self.program_counter, ea, self.cfg.mem_info.get_shortest(ea)
#            ))
            self.program_counter = ea
#         else:
#            log.debug("$%x BLE: don't branch to $%x, because N^V!=1 and Z!=1 \t| %s" % (
#                self.program_counter, ea, self.cfg.mem_info.get_shortest(ea)
#            ))

    @opcode(# Branch if lower or same (unsigned)
        0x23, # BLS (relative)
        0x1023, # LBLS (relative)
    )
    def instruction_BLS(self, opcode, ea):
        """
        Causes a branch if the previous operation caused either a carry or a
        zero result. When used after a subtract or compare operation on unsigned
        binary values, this instruction will branch if the register was lower
        than or the same as the memory register.

        Generally not useful after INC/DEC, LD/ST, and TST/CLR/COM instructions.

        source code forms: BLS dd; LBLS DDDD

        CC bits "HNZVC": -----
        """
#         if (self.cc.C|self.cc.Z) == 0:
        if self.cc.C == 1 or self.cc.Z == 1:
#            log.info("$%x BLS branch to $%x, because C|Z==1 \t| %s" % (
#                self.program_counter, ea, self.cfg.mem_info.get_shortest(ea)
#            ))
            self.program_counter = ea
#         else:
#            log.debug("$%x BLS: don't branch to $%x, because C|Z!=1 \t| %s" % (
#                self.program_counter, ea, self.cfg.mem_info.get_shortest(ea)
#            ))

    @opcode(# Branch if less than (signed)
        0x2d, # BLT (relative)
        0x102d, # LBLT (relative)
    )
    def instruction_BLT(self, opcode, ea):
        """
        Causes a branch if either, but not both, of the N (negative) or V
        (overflow) bits is set. That is, branch if the sign of a valid twos
        complement result is, or would be, negative. When used after a subtract
        or compare operation on twos complement binary values, this instruction
        will branch if the register was less than the memory register.

        source code forms: BLT dd; LBLT DDDD

        CC bits "HNZVC": -----
        """
        if (self.cc.N ^ self.cc.V) == 1: # N xor V
#            log.info("$%x BLT branch to $%x, because N XOR V == 1 \t| %s" % (
#                self.program_counter, ea, self.cfg.mem_info.get_shortest(ea)
#            ))
            self.program_counter = ea
#         else:
#            log.debug("$%x BLT: don't branch to $%x, because N XOR V != 1 \t| %s" % (
#                self.program_counter, ea, self.cfg.mem_info.get_shortest(ea)
#            ))

    @opcode(# Branch if minus
        0x2b, # BMI (relative)
        0x102b, # LBMI (relative)
    )
    def instruction_BMI(self, opcode, ea):
        """
        Tests the state of the N (negative) bit and causes a branch if set. That
        is, branch if the sign of the twos complement result is negative.

        When used after an operation on signed binary values, this instruction
        will branch if the result is minus. It is generally preferred to use the
        LBLT instruction after signed operations.

        source code forms: BMI dd; LBMI DDDD

        CC bits "HNZVC": -----
        """
        if self.cc.N == 1:
#            log.info("$%x BMI branch to $%x, because N==1 \t| %s" % (
#                self.program_counter, ea, self.cfg.mem_info.get_shortest(ea)
#            ))
            self.program_counter = ea
#         else:
#            log.debug("$%x BMI: don't branch to $%x, because N==0 \t| %s" % (
#                self.program_counter, ea, self.cfg.mem_info.get_shortest(ea)
#            ))

    @opcode(# Branch if not equal
        0x26, # BNE (relative)
        0x1026, # LBNE (relative)
    )
    def instruction_BNE(self, opcode, ea):
        """
        Tests the state of the Z (zero) bit and causes a branch if it is clear.
        When used after a subtract or compare operation on any binary values,
        this instruction will branch if the register is, or would be, not equal
        to the memory register.

        source code forms: BNE dd; LBNE DDDD

        CC bits "HNZVC": -----
        """
        if self.cc.Z == 0:
#            log.info("$%x BNE branch to $%x, because Z==0 \t| %s" % (
#                self.program_counter, ea, self.cfg.mem_info.get_shortest(ea)
#            ))
            self.program_counter = ea
#        else:
#            log.debug("$%x BNE: don't branch to $%x, because Z==1 \t| %s" % (
#                self.program_counter, ea, self.cfg.mem_info.get_shortest(ea)
#            ))

    @opcode(# Branch if plus
        0x2a, # BPL (relative)
        0x102a, # LBPL (relative)
    )
    def instruction_BPL(self, opcode, ea):
        """
        Tests the state of the N (negative) bit and causes a branch if it is
        clear. That is, branch if the sign of the twos complement result is
        positive.

        When used after an operation on signed binary values, this instruction
        will branch if the result (possibly invalid) is positive. It is
        generally preferred to use the BGE instruction after signed operations.

        source code forms: BPL dd; LBPL DDDD

        CC bits "HNZVC": -----
        """
        if self.cc.N == 0:
#            log.info("$%x BPL branch to $%x, because N==0 \t| %s" % (
#                self.program_counter, ea, self.cfg.mem_info.get_shortest(ea)
#            ))
            self.program_counter = ea
#         else:
#            log.debug("$%x BPL: don't branch to $%x, because N==1 \t| %s" % (
#                self.program_counter, ea, self.cfg.mem_info.get_shortest(ea)
#            ))

    @opcode(# Branch always
        0x20, # BRA (relative)
        0x16, # LBRA (relative)
    )
    def instruction_BRA(self, opcode, ea):
        """
        Causes an unconditional branch.

        source code forms: BRA dd; LBRA DDDD

        CC bits "HNZVC": -----
        """
        # FIXME: remove speedup Simple6809 RAM test
#         if self.cfg.__class__.__name__ == "Simple6809Cfg":
#             if self.program_counter == 0xdb79 and ea == 0xdb6a: # RAM size test loop
# #                 msg = repr(["%x" % x for x in [self.program_counter, ea, m, self.index_x.get()]])
# #                 raise RuntimeError(msg)
#                 new_x = 0x7ffd
#                 new_ea = 0xdb79
#                 log.warn(
#                     "Speedup Simple6809 RAM test: Set X to $%x and goto $%x" % (
#                         new_x, new_ea
#                 ))
#                 self.index_x.set(new_x)
#                 self.program_counter = new_ea
#                 return

#        log.info("$%x BRA branch to $%x \t| %s" % (
#            self.program_counter, ea, self.cfg.mem_info.get_shortest(ea)
#        ))
        self.program_counter = ea

    @opcode(# Branch never
        0x21, # BRN (relative)
        0x1021, # LBRN (relative)
    )
    def instruction_BRN(self, opcode, ea):
        """
        Does not cause a branch. This instruction is essentially a no operation,
        but has a bit pattern logically related to branch always.

        source code forms: BRN dd; LBRN DDDD

        CC bits "HNZVC": -----
        """
        pass

    @opcode(# Branch if valid twos complement result
        0x28, # BVC (relative)
        0x1028, # LBVC (relative)
    )
    def instruction_BVC(self, opcode, ea):
        """
        Tests the state of the V (overflow) bit and causes a branch if it is
        clear. That is, branch if the twos complement result was valid. When
        used after an operation on twos complement binary values, this
        instruction will branch if there was no overflow.

        source code forms: BVC dd; LBVC DDDD

        CC bits "HNZVC": -----
        """
        if self.cc.V == 0:
#            log.info("$%x BVC branch to $%x, because V==0 \t| %s" % (
#                self.program_counter, ea, self.cfg.mem_info.get_shortest(ea)
#            ))
            self.program_counter = ea
#         else:
#            log.debug("$%x BVC: don't branch to $%x, because V==1 \t| %s" % (
#                self.program_counter, ea, self.cfg.mem_info.get_shortest(ea)
#            ))

    @opcode(# Branch if invalid twos complement result
        0x29, # BVS (relative)
        0x1029, # LBVS (relative)
    )
    def instruction_BVS(self, opcode, ea):
        """
        Tests the state of the V (overflow) bit and causes a branch if it is
        set. That is, branch if the twos complement result was invalid. When
        used after an operation on twos complement binary values, this
        instruction will branch if there was an overflow.

        source code forms: BVS dd; LBVS DDDD

        CC bits "HNZVC": -----
        """
        if self.cc.V == 1:
#            log.info("$%x BVS branch to $%x, because V==1 \t| %s" % (
#                self.program_counter, ea, self.cfg.mem_info.get_shortest(ea)
#            ))
            self.program_counter = ea
#         else:
#            log.debug("$%x BVS: don't branch to $%x, because V==0 \t| %s" % (
#                self.program_counter, ea, self.cfg.mem_info.get_shortest(ea)
#            ))

    @opcode(# Branch if lower (unsigned)
        0x25, # BLO/BCS (relative)
        0x1025, # LBLO/LBCS (relative)
    )
    def instruction_BLO(self, opcode, ea):
        """
        CC bits "HNZVC": -----
        case 0x5: cond = REG_CC & CC_C; break; // BCS, BLO, LBCS, LBLO
        """
        if self.cc.C == 1:
#            log.info("$%x BLO/BCS/LBLO/LBCS branch to $%x, because C==1 \t| %s" % (
#                self.program_counter, ea, self.cfg.mem_info.get_shortest(ea)
#            ))
            self.program_counter = ea
#         else:
#            log.debug("$%x BLO/BCS/LBLO/LBCS: don't branch to $%x, because C==0 \t| %s" % (
#                self.program_counter, ea, self.cfg.mem_info.get_shortest(ea)
#            ))

    @opcode(# Branch if lower (unsigned)
        0x24, # BHS/BCC (relative)
        0x1024, # LBHS/LBCC (relative)
    )
    def instruction_BHS(self, opcode, ea):
        """
        CC bits "HNZVC": -----
        case 0x4: cond = !(REG_CC & CC_C); break; // BCC, BHS, LBCC, LBHS
        """
        if self.cc.C == 0:
#            log.info("$%x BHS/BCC/LBHS/LBCC branch to $%x, because C==0 \t| %s" % (
#                self.program_counter, ea, self.cfg.mem_info.get_shortest(ea)
#            ))
            self.program_counter = ea
#        else:
#            log.debug("$%x BHS/BCC/LBHS/LBCC: don't branch to $%x, because C==1 \t| %s" % (
#                self.program_counter, ea, self.cfg.mem_info.get_shortest(ea)
#            ))


    # ---- Logical shift: LSL, LSR ----


    def LSL(self, a):
        """
        Shifts all bits of accumulator A or B or memory location M one place to
        the left. Bit zero is loaded with a zero. Bit seven of accumulator A or
        B or memory location M is shifted into the C (carry) bit.

        This is a duplicate assembly-language mnemonic for the single machine
        instruction ASL.

        source code forms: LSL Q; LSLA; LSLB

        CC bits "HNZVC": naaas
        """
        r = a << 1
        self.cc.clear_NZVC()
        self.cc.update_NZVC_8(a, a, r)
        return r # XXX: & 0xff

    @opcode(0x8, 0x68, 0x78) # LSL/ASL (direct, indexed, extended)
    def instruction_LSL_memory(self, opcode, ea, m):
        """
        Logical shift left memory location / Arithmetic shift of memory left
        """
        r = self.LSL(m)
#        log.debug("$%x LSL memory value $%x << 1 = $%x and write it to $%x \t| %s" % (
#            self.program_counter,
#            m, r, ea,
#            self.cfg.mem_info.get_shortest(ea)
#        ))
        return ea, r & 0xff

    @opcode(0x48, 0x58) # LSLA/ASLA / LSLB/ASLB (inherent)
    def instruction_LSL_register(self, opcode, register):
        """
        Logical shift left accumulator / Arithmetic shift of accumulator
        """
        a = register.get()
        r = self.LSL(a)
#        log.debug("$%x LSL %s value $%x << 1 = $%x" % (
#            self.program_counter,
#            register.name, a, r
#        ))
        register.set(r)

    def LSR(self, a):
        """
        Performs a logical shift right on the register. Shifts a zero into bit
        seven and bit zero into the C (carry) bit.

        source code forms: LSR Q; LSRA; LSRB

        CC bits "HNZVC": -0a-s
        """
        r = a >> 1
        self.cc.clear_NZC()
        self.cc.C |= (a & 1) # XXX: ok?
        self.cc.set_Z8(r)
        return r # XXX: & 0xff

    @opcode(0x4, 0x64, 0x74) # LSR (direct, indexed, extended)
    def instruction_LSR_memory(self, opcode, ea, m):
        """ Logical shift right memory location """
        r = self.LSR(m)
#        log.debug("$%x LSR memory value $%x >> 1 = $%x and write it to $%x \t| %s" % (
#            self.program_counter,
#            m, r, ea,
#            self.cfg.mem_info.get_shortest(ea)
#        ))
        return ea, r & 0xff

    @opcode(0x44, 0x54) # LSRA / LSRB (inherent)
    def instruction_LSR_register(self, opcode, register):
        """ Logical shift right accumulator """
        a = register.get()
        r = self.LSR(a)
#        log.debug("$%x LSR %s value $%x >> 1 = $%x" % (
#            self.program_counter,
#            register.name, a, r
#        ))
        register.set(r)

    def ASR(self, a):
        """
        ASR (Arithmetic Shift Right) alias LSR (Logical Shift Right)

        Shifts all bits of the register one place to the right. Bit seven is held
        constant. Bit zero is shifted into the C (carry) bit.

        source code forms: ASR Q; ASRA; ASRB

        CC bits "HNZVC": uaa-s
        """
        r = (a >> 1) | (a & 0x80)
        self.cc.clear_NZC()
        self.cc.C |= (a & 1)
        self.cc.update_NZ_8(r)
        return r

    @opcode(0x7, 0x67, 0x77) # ASR (direct, indexed, extended)
    def instruction_ASR_memory(self, opcode, ea, m):
        """ Arithmetic shift memory right """
        r = self.ASR(m)
#        log.debug("$%x ASR memory value $%x >> 1 | Carry = $%x and write it to $%x \t| %s" % (
#            self.program_counter,
#            m, r, ea,
#            self.cfg.mem_info.get_shortest(ea)
#        ))
        return ea, r & 0xff

    @opcode(0x47, 0x57) # ASRA/ASRB (inherent)
    def instruction_ASR_register(self, opcode, register):
        """ Arithmetic shift accumulator right """
        a = register.get()
        r = self.ASR(a)
#        log.debug("$%x ASR %s value $%x >> 1 | Carry = $%x" % (
#            self.program_counter,
#            register.name, a, r
#        ))
        register.set(r)


    # ---- Rotate: ROL, ROR ----


    def ROL(self, a):
        """
        Rotates all bits of the register one place left through the C (carry)
        bit. This is a 9-bit rotation.

        source code forms: ROL Q; ROLA; ROLB

        CC bits "HNZVC": -aaas
        """
        r = (a << 1) | self.cc.C
        self.cc.clear_NZVC()
        self.cc.update_NZVC_8(a, a, r)
        return r

    @opcode(0x9, 0x69, 0x79) # ROL (direct, indexed, extended)
    def instruction_ROL_memory(self, opcode, ea, m):
        """ Rotate memory left """
        r = self.ROL(m)
#        log.debug("$%x ROL memory value $%x << 1 | Carry = $%x and write it to $%x \t| %s" % (
#            self.program_counter,
#            m, r, ea,
#            self.cfg.mem_info.get_shortest(ea)
#        ))
        return ea, r & 0xff

    @opcode(0x49, 0x59) # ROLA / ROLB (inherent)
    def instruction_ROL_register(self, opcode, register):
        """ Rotate accumulator left """
        a = register.get()
        r = self.ROL(a)
#        log.debug("$%x ROL %s value $%x << 1 | Carry = $%x" % (
#            self.program_counter,
#            register.name, a, r
#        ))
        register.set(r)

    def ROR(self, a):
        """
        Rotates all bits of the register one place right through the C (carry)
        bit. This is a 9-bit rotation.

        moved the carry flag into bit 8
        moved bit 7 into carry flag

        source code forms: ROR Q; RORA; RORB

        CC bits "HNZVC": -aa-s
        """
        r = (a >> 1) | (self.cc.C << 7)
        self.cc.clear_NZ()
        self.cc.update_NZ_8(r)
        self.cc.C = a & 1
        return r # XXX: & 0xff

    @opcode(0x6, 0x66, 0x76) # ROR (direct, indexed, extended)
    def instruction_ROR_memory(self, opcode, ea, m):
        """ Rotate memory right """
        r = self.ROR(m)
#        log.debug("$%x ROR memory value $%x >> 1 | Carry = $%x and write it to $%x \t| %s" % (
#            self.program_counter,
#            m, r, ea,
#            self.cfg.mem_info.get_shortest(ea)
#        ))
        return ea, r & 0xff

    @opcode(0x46, 0x56) # RORA/RORB (inherent)
    def instruction_ROR_register(self, opcode, register):
        """ Rotate accumulator right """
        a = register.get()
        r = self.ROR(a)
#        log.debug("$%x ROR %s value $%x >> 1 | Carry = $%x" % (
#            self.program_counter,
#            register.name, a, r
#        ))
        register.set(r)


    # ---- Not Implemented, yet. ----


    @opcode(# AND condition code register, then wait for interrupt
        0x3c, # CWAI (immediate)
    )
    def instruction_CWAI(self, opcode, m):
        """
        This instruction ANDs an immediate byte with the condition code register
        which may clear the interrupt mask bits I and F, stacks the entire
        machine state on the hardware stack and then looks for an interrupt.
        When a non-masked interrupt occurs, no further machine state information
        need be saved before vectoring to the interrupt handling routine. This
        instruction replaced the MC6800 CLI WAI sequence, but does not place the
        buses in a high-impedance state. A FIRQ (fast interrupt request) may
        enter its interrupt handler with its entire machine state saved. The RTI
        (return from interrupt) instruction will automatically return the entire
        machine state after testing the E (entire) bit of the recovered
        condition code register.

        The following immediate values will have the following results: FF =
        enable neither EF = enable IRQ BF = enable FIRQ AF = enable both

        source code forms: CWAI #$XX E F H I N Z V C

        CC bits "HNZVC": ddddd
        """
        log.error("$%x CWAI not implemented, yet!", opcode)
        # Update CC bits: ddddd

    @opcode(# Undocumented opcode!
        0x3e, # RESET (inherent)
    )
    def instruction_RESET(self, opcode):
        """
        Build the ASSIST09 vector table and setup monitor defaults, then invoke
        the monitor startup routine.

        source code forms:

        CC bits "HNZVC": *****
        """
        raise NotImplementedError("$%x RESET" % opcode)
        # Update CC bits: *****

    @opcode(# Return from interrupt
        0x3b, # RTI (inherent)
    )
    def instruction_RTI(self, opcode):
        """
        The saved machine state is recovered from the hardware stack and control
        is returned to the interrupted program. If the recovered E (entire) bit
        is clear, it indicates that only a subset of the machine state was saved
        (return address and condition codes) and only that subset is recovered.

        source code forms: RTI

        CC bits "HNZVC": -----
        """
        raise NotImplementedError("$%x RTI" % opcode)

    @opcode(# Software interrupt (absolute indirect)
        0x3f, # SWI (inherent)
    )
    def instruction_SWI(self, opcode):
        """
        All of the processor registers are pushed onto the hardware stack (with
        the exception of the hardware stack pointer itself), and control is
        transferred through the software interrupt vector. Both the normal and
        fast interrupts are masked (disabled).

        source code forms: SWI

        CC bits "HNZVC": -----
        """
        raise NotImplementedError("$%x SWI" % opcode)

    @opcode(# Software interrupt (absolute indirect)
        0x103f, # SWI2 (inherent)
    )
    def instruction_SWI2(self, opcode, ea, m):
        """
        All of the processor registers are pushed onto the hardware stack (with
        the exception of the hardware stack pointer itself), and control is
        transferred through the software interrupt 2 vector. This interrupt is
        available to the end user and must not be used in packaged software.
        This interrupt does not mask (disable) the normal and fast interrupts.

        source code forms: SWI2

        CC bits "HNZVC": -----
        """
        raise NotImplementedError("$%x SWI2" % opcode)

    @opcode(# Software interrupt (absolute indirect)
        0x113f, # SWI3 (inherent)
    )
    def instruction_SWI3(self, opcode, ea, m):
        """
        All of the processor registers are pushed onto the hardware stack (with
        the exception of the hardware stack pointer itself), and control is
        transferred through the software interrupt 3 vector. This interrupt does
        not mask (disable) the normal and fast interrupts.

        source code forms: SWI3

        CC bits "HNZVC": -----
        """
        raise NotImplementedError("$%x SWI3" % opcode)

    @opcode(# Synchronize with interrupt line
        0x13, # SYNC (inherent)
    )
    def instruction_SYNC(self, opcode):
        """
        FAST SYNC WAIT FOR DATA Interrupt! LDA DISC DATA FROM DISC AND CLEAR
        INTERRUPT STA ,X+ PUT IN BUFFER DECB COUNT IT, DONE? BNE FAST GO AGAIN
        IF NOT.

        source code forms: SYNC

        CC bits "HNZVC": -----
        """
        raise NotImplementedError("$%x SYNC" % opcode)


def test_run():
    print "test run..."
    import subprocess
    cmd_args = [sys.executable,
        os.path.join("..", "DragonPy_CLI.py"),
#         "--verbosity=5",
#         "--verbosity=10", # DEBUG
#         "--verbosity=20", # INFO
#         "--verbosity=30", # WARNING
#         "--verbosity=40", # ERROR
#         "--verbosity=50", # CRITICAL/FATAL
#
#         '--log_formatter=%(filename)s %(funcName)s %(lineno)d %(message)s',
#
#         "--area_debug_active=5:bb79-ffff",
#         "--area_debug_cycles=1587101",
#
#         "--cfg=sbc09",
#          "--cfg=Simple6809",
#        "--cfg=Dragon32",

         "--cfg=Multicomp6809",

#
#         "--compare_trace=2", # PC differ
#
#         "--max=15000",
#         "--max=46041",
    ]
    print "Startup CLI with: %s" % " ".join(cmd_args[1:])
    subprocess.Popen(cmd_args).wait()
    sys.exit(0)


if __name__ == "__main__":
    from dragonpy.DragonPy_CLI import get_cli
    cli = get_cli()

    if not cli.cfg.use_bus:
        print "DragonPy cpu core"
        print "Run DragonPy_CLI.py instead"
        test_run()
        sys.exit(0)

    bus_socket_addr = cli.cfg.bus_socket_addr

    print "Connect to internal socket bus I/O %s" % repr(bus_socket_addr)
    assert isinstance(bus_socket_addr, tuple)
    bus = socket.socket()
    bus.connect(bus_socket_addr)
    assert bus is not None
    cli.cfg.bus = bus

    cpu = CPU(cli.cfg)
    cpu.reset()
    try:
        cpu.run()
    except:
        print_exc_plus()

