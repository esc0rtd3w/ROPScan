#!/usr/bin/env python
# vim: set et ts=4
"""
This script uses objdump to extract code section(s) from a
binary and then disassembles sequences ending with C3 to
determine if this sequence would be a valid return sequence
used for a return-oriented-programming-based attack.
"""

import sys
import re
import os
import hashlib

import scriptine
import scriptine.shell
import scriptine.log

from bdutil import abstract, Colors


class CommandChecker():
    """
    Class for building shell command lines used by the program
    """
    def __init__(self):
        self.prerequisites = [ "udcli", "objdump", "readelf" ]


    def prereq_check(self):
        """Check if all required prerequisites for the shell-tool-based version
           are available."""

        # prerequisites to check for
        prereqs = ["udcli",
                   "objdump",
                   "readelf"]

        for pre in prereqs:
            res = scriptine.shell.backtick("which %s" % pre)
            if res == "":
                scriptine.log.warn("%s%s%s not found", Colors.Yellow,
                                   pre, Colors.Reset)
                return False

        return True


class Cmd:
    """
    Abstract command class
    """
    def __init__(self):     abstract()
    def cmd_str(self):      abstract()
    def parse_result(self): abstract()


class ReadelfCmd(Cmd):
    def __init__(self):
        pass

    def cmd_str(self, binaryfile, section, tmpfile):
        """
        Generate call to readelf extracting segment list
        """
        cmd = "readelf -S %s | " % binaryfile
        cmd += "grep %s > %s" % (section, tmpfile)
        return cmd


    def parse_result(self, tmpfile, sec_name):
        """Scan readelf result for start and size of .text segment"""
        start = -1
        size  = -1

        res = file(tmpfile) .readlines()
        elements = res[0].split()

        # Depending on the position of .text in the section list, splitting
        # the result line will give a varying count of elements.
        # Start and size are at fixed indices from the position of .text, though
        base_index = elements.index(sec_name)

        start = int(elements[base_index + 2], 16)
        size  = int(elements[base_index + 4], 16)

        scriptine.log.info("Start %08x Size %08x", start, size)
        return (start, size)


class ObjdumpCmd(Cmd):
    """
    Objdump command class
    """
    def __init__(self):
        self.BYTE8_RE = re.compile("([a-f0-9]{2})([a-f0-9]{2})([a-f0-9]{2})([a-f0-9]{2})")


    def cmd_str(self, start_addr, segment_size, binaryfile, tmpfile):
        """
        Generate call to objdump extracting bytes between start and start+size
        """
        cmd = "objdump -s "
        cmd += "--start-address=0x%08x " % start_addr
        cmd += "--stop-address=0x%08x " % (start_addr+segment_size)
        cmd += "%s >%s" % (binaryfile, tmpfile)
        return cmd


    def parse_result(self, tmpfile):
        """Extract the opcode bytes from objdump's output stream"""
        stream = []

        tmpf = file(tmpfile)

        for line in tmpf.readlines():
            bytes = line.split()

            # Strip unneeded output:
            if len(bytes) == 0:
                continue
            if not self.BYTE8_RE.match(bytes[1]):
                scriptine.log.debug("Dropping %s", bytes)
                continue

            # extract inner 4 columns
            for data in bytes[1:5]:
                match = self.BYTE8_RE.match(data)
                if match:
                    stream += match.groups()
                else:
                    pass

        return stream


class UDCLICmd(Cmd):
    """
    Command for the UDCLI disassembler
    """
    def __init__(self):
        pass

    def cmd_str(self, data, tmpfile):
        """
        Generate call to UDCLI disassembler
        """
        cmd = "echo %s |  udcli -x -32 -noff -nohex >%s" % (data, tmpfile)
        return cmd

    def parse_result(self):
        pass


class OpcodeStream():
    def __init__(self, bytestream):
        self.stream = bytestream

    def find_sequences(self, byte_offs=20,
                       opcode="c3", opcode_str="ret"):
        """
        Run through byte stream, find occurences of opcode and check if the
        corresponding disassembly for the last [1, byte_offs] bytes ends with
        a ret instruction.

        Returns: List of (offset, length) tuples representing the valid sequences.
        """

        tmpfile = "foo.tmp"
        ret = []

        # we need at least one more byte than the C3 instruction,
        # so less than 2 is bad
        if (byte_offs < 2):
            scriptine.log.error("Byte offset (%d) too small.", byte_offs)
            return ret

        scriptine.log.log("Scanning byte stream for %s instruction sequences",
                          opcode)

        # find first occurence
        idx = self.stream.index(opcode)
        while idx > 0:
            streampos_str = "Position in stream: %02.2f%%" % (100.0 * float(idx+1) / len(self.stream))
            print streampos_str,

            # if occurence is less than byte_offs bytes into the stream,
            # adapt the limit
            if idx > byte_offs:
                limit = byte_offs
            else:
                limit = idx+1

            # validity check sequences by disassembling
            for i in range(1, limit):
                # byte string to send to disassembler
                byte_data = "".join([c+' ' for c in self.stream[idx-i: idx+1]])

                udcli = UDCLICmd()
                cmd = udcli.cmd_str(byte_data, tmpfile)
                res = scriptine.shell.sh(cmd)
                if res != 0:
                    print cmd, res

                # sequence is valid, if tmpfile contains opcode_str in the last line
                tmpf = file(tmpfile)
                lines = [l.strip() for l in tmpf.readlines()]

                # find first occurrence of RET in disassembly
                try:
                    finishing_idx = lines.index(opcode_str)
                except: # might even have NO ret at all
                    finishing_idx = -1

                # -> this sequence is a valid new sequence, iff RET only occurs as
                #    the final instruction
                if finishing_idx == len(lines)-1:
                    ret += [(idx, i)]

            try:
                idx = self.stream.index(opcode, idx+1)
            except:
                idx = -1

            # N chars left, 1 up
            print("\033[%dD\033[A" % len(streampos_str))

        os.remove(tmpfile)
        print ""

        return ret


    def unique_sequences(self, locations):
        """
        Given a byte stream and a set of locations, determine how
        many unique sequences are within the stream.
        """
        uniq_seqs = set()
        for (off, length) in locations:
            seq = "".join(self.stream[off-length:off+1])
            hsh = hashlib.md5(seq)
            uniq_seqs.add(hsh.hexdigest())

        return uniq_seqs


    def dump_byte_stream(self, offset, length):
        """
        Dump byte stream at given (offset, length) pairs.

        Note: length in (offset, length) means _before_ offset
        """
        print " ".join(self.stream[offset:offset+length+1]),


    def dump_locations_with_offset(self, locations, start_offset):
        for (c3_offset, length) in locations:
            begin = start_offset + c3_offset - length
            print "0x%08x + %3d:  %s" % (begin, length, Colors.Cyan),
            self.dump_byte_stream(c3_offset-length, length)
            print "%s" % (Colors.Reset)


def scan_command(filename, dump="yes", numbytes=20):
    """
    Shell command: scan binary for C3 instruction sequences

    Options:
       filename  -- binary to scan
       dump      -- dump sequences (yes/no), default: yes
    """
    section_name = ".text" # we search for the text section as this is
                           # the one we'd like to scan through
    tmpfile = "foo.tmp"

    # run readelf -S on the file to find the section info
    readelf = ReadelfCmd()
    cmd = readelf.cmd_str(filename, section_name, tmpfile)
    res = scriptine.shell.sh(cmd)
    if res != 0:
        scriptine.log.error("%sreadelf error%s", Colors.Red,
                            Colors.Reset)
        return

    (start, size) = readelf.parse_result(tmpfile, section_name)
    if (start == -1 and size == -1):
        scriptine.log.error("%sCannot determine start/size of .text section%s",
                            Colors.Red, Colors.Reset)
        return

    # use (start, size) to objdump text segment and extract opcode stream
    objdump = ObjdumpCmd()
    cmd = objdump.cmd_str(start, size, filename, tmpfile)
    res = scriptine.shell.sh(cmd)
    if res != 0:
        scriptine.log.error("%sError in objdump%s", Colors.Red,
                            Colors.Reset)

    stream = objdump.parse_result(tmpfile)
    if len(stream) == 0:
        scriptine.log.error("%sEmpty instruction stream?%s",
                            Colors.Red, Colors.Reset)

    scriptine.log.info("Stream bytes: %d, real size %d", len(stream), size)
    # we must have extracted all bytes
    if len(stream) != size:
        print stream
        sys.exit(1)

    os.remove(tmpfile)

    ostream = OpcodeStream(stream)
    # analyze stream
    locations = ostream.find_sequences(opcode="c3", opcode_str="ret", byte_offs=numbytes)
    scriptine.log.log("Found: %d sequences.", len(locations))

    # check for uniqueness of sequences
    uniq_seqs = ostream.unique_sequences(locations)
    scriptine.log.log("       %d unique sequences", len(uniq_seqs))

    # get unique locations by creating a set of offsets
    c3_locs = set([offs for (offs,length) in locations])
    scriptine.log.log("       %d unique C3 locations", len(c3_locs))

    if dump == "yes":
        ostream.dump_locations_with_offset(locations, start)



if __name__ == "__main__":
    if (CommandChecker().prereq_check()):
        scriptine.run()
    else:
        scriptine.log.error(
            "Missing shell tool(s). No native implementation (yet).")
