#!/usr/bin/env python3
"""Minimal, dependency-free ELF64 little-endian reader/writer.

Enough of the format to read PT_INTERP, DT_NEEDED, DT_SONAME, DT_RPATH and
DT_RUNPATH, and to overwrite an rpath string in place.  Deliberately no
pyelftools and no patchelf: patchelf costs a fork+exec per file, which dominates
the cost of relocating a large install tree.
"""
import os
import struct

PT_LOAD, PT_DYNAMIC, PT_INTERP = 1, 2, 3
DT_NULL, DT_NEEDED, DT_STRTAB, DT_STRSZ = 0, 1, 5, 10
DT_SONAME, DT_RPATH, DT_RUNPATH = 14, 15, 29

TAGNAME = {DT_RPATH: "DT_RPATH", DT_RUNPATH: "DT_RUNPATH"}


class NotElf(Exception):
    pass


class Elf64:
    def __init__(self, path, data=None):
        self.path = path
        self.data = data if data is not None else open(path, "rb").read()
        d = self.data
        if len(d) < 64 or d[:4] != b"\x7fELF":
            raise NotElf(path)
        if d[4] != 2 or d[5] != 1:          # ELF64 little-endian only
            raise NotElf("%s: not ELF64/LE" % path)
        self.e_type, self.e_machine = struct.unpack_from("<HH", d, 16)
        e_phoff, = struct.unpack_from("<Q", d, 32)
        e_phentsize, e_phnum = struct.unpack_from("<HH", d, 54)

        self.loads = []
        self.interp = None
        self._dyn_span = None
        for i in range(e_phnum):
            o = e_phoff + i * e_phentsize
            if o + 56 > len(d):
                break
            p_type, _flags, p_off, p_vaddr, _pa, p_filesz, _ms, _al = \
                struct.unpack_from("<IIQQQQQQ", d, o)
            if p_type == PT_LOAD:
                self.loads.append((p_vaddr, p_filesz, p_off))
            elif p_type == PT_DYNAMIC:
                self._dyn_span = (p_off, p_filesz)
            elif p_type == PT_INTERP:
                self.interp = d[p_off:p_off + p_filesz].split(b"\0")[0].decode("utf-8", "replace")

        self.entries = []       # (file_offset_of_entry, d_tag, d_val)
        self.strtab_off = None
        self.strsz = 0
        if self._dyn_span:
            self._parse_dynamic()

    # -- addresses ----------------------------------------------------------
    def v2o(self, vaddr):
        for base, filesz, off in self.loads:
            if base <= vaddr < base + filesz:
                return off + (vaddr - base)
        return None

    def _parse_dynamic(self):
        off, size = self._dyn_span
        d = self.data
        strtab_vaddr = None
        pos = off
        while pos + 16 <= min(off + size, len(d)):
            tag, val = struct.unpack_from("<qQ", d, pos)
            self.entries.append((pos, tag, val))
            if tag == DT_NULL:
                break
            if tag == DT_STRTAB:
                strtab_vaddr = val
            elif tag == DT_STRSZ:
                self.strsz = val
            pos += 16
        if strtab_vaddr is not None:
            self.strtab_off = self.v2o(strtab_vaddr)

    # -- strings ------------------------------------------------------------
    def getstr(self, index):
        if self.strtab_off is None:
            return None
        start = self.strtab_off + index
        end = self.data.find(b"\0", start)
        if end < 0:
            return None
        return self.data[start:end].decode("utf-8", "replace")

    def dynstr_blob(self):
        if self.strtab_off is None or not self.strsz:
            return b""
        return self.data[self.strtab_off:self.strtab_off + self.strsz]

    # -- dynamic tags -------------------------------------------------------
    def needed(self):
        return [self.getstr(v) for _o, t, v in self.entries if t == DT_NEEDED]

    def soname(self):
        for _o, t, v in self.entries:
            if t == DT_SONAME:
                return self.getstr(v)
        return None

    def rpath_entries(self):
        """[{tag, tagname, entry_off, str_index, str_off, value}] for RPATH/RUNPATH."""
        out = []
        for off, tag, val in self.entries:
            if tag in (DT_RPATH, DT_RUNPATH) and self.strtab_off is not None:
                out.append({
                    "tag": tag,
                    "tagname": TAGNAME[tag],
                    "entry_off": off,
                    "str_index": val,
                    "str_off": self.strtab_off + val,
                    "value": self.getstr(val) or "",
                })
        return out
