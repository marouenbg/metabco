"""Repair the UTF-8 declaration bug in AGORA2's published SBML.

AGORA2 v2.01 (sbml_files_fixed, 2024-07-04) ships XML that declares
`encoding="UTF-8"` while containing raw Latin-1 bytes. The one we hit first is
0xDF -- the sharp s in "regorafenib-N-ss-glucuronide" -- but any metabolite name
carrying a Greek letter or an accent does it. Eleven of the thirty
reconstructions in our roster are affected, with nine bad sequences in
B. thetaiotaomicron alone. libsbml rejects the file outright ("Badly formed XML")
so cobrapy cannot read it at all.

The extraction is not at fault: size and CRC both match the archive exactly.
This is how the files were published.

The repair is surgical rather than wholesale. Decoding the entire file as
Latin-1 and re-encoding would corrupt any sequence that IS valid UTF-8, turning
a correct e-acute into mojibake. Instead the decoder walks the file and converts
only the bytes that fail UTF-8, interpreting each as Latin-1 -- which is what the
producing tool (a MATLAB export) actually wrote.

Originals are never modified in place: repaired copies go to a sibling
directory, so the published files stay available for comparison.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def transcode(raw: bytes) -> tuple[bytes, int]:
    """Return (valid UTF-8 bytes, number of bytes repaired)."""
    out = bytearray()
    i, n_fixed = 0, 0
    while i < len(raw):
        b = raw[i]
        if b < 0x80:                       # plain ASCII, the overwhelming majority
            out.append(b)
            i += 1
            continue
        # try to consume a valid multi-byte UTF-8 sequence starting here
        for width in (2, 3, 4):
            chunk = raw[i:i + width]
            if len(chunk) < width:
                continue
            try:
                chunk.decode("utf-8")
            except UnicodeDecodeError:
                continue
            out.extend(chunk)
            i += width
            break
        else:
            # not valid UTF-8: the producer wrote a Latin-1 byte
            out.extend(bytes([b]).decode("latin-1").encode("utf-8"))
            i += 1
            n_fixed += 1
    return bytes(out), n_fixed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="data/gems/agora2_v201")
    ap.add_argument("--dst", default="data/gems/agora2_v201_utf8")
    args = ap.parse_args()
    src, dst = Path(args.src), Path(args.dst)
    dst.mkdir(parents=True, exist_ok=True)

    n_files = n_bad = total_fixed = 0
    for f in sorted(src.glob("*.xml")):
        raw = f.read_bytes()
        n_files += 1
        try:
            raw.decode("utf-8")
            (dst / f.name).write_bytes(raw)          # already clean, copy through
            continue
        except UnicodeDecodeError:
            pass
        fixed, n = transcode(raw)
        fixed.decode("utf-8")                        # must now be valid, or raise
        (dst / f.name).write_bytes(fixed)
        n_bad += 1
        total_fixed += n
        print(f"  {f.name:52s} {n:3d} bytes repaired")

    print(f"\n{n_files} files, {n_bad} needed repair, {total_fixed} bytes total")
    print(f"written -> {dst}")


if __name__ == "__main__":
    main()
