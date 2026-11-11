#!/usr/bin/env python3
"""
YTDISK – Store files inside 4K YouTube videos (educational)

Improvements in this version:
- Optional zlib compression before encryption (often free capacity)
- Capacity estimator command
- Better progress reporting (tqdm if available)
- More CLI options (crf, no-upscale, compress, verbose)
- Improved error messages and header recovery
- Cleaner structure + type hints
- Version bump to 2
"""

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import time
import zlib
from pathlib import Path
from typing import Optional, Tuple, Dict, Any

import numpy as np

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
GRID_W, GRID_H = 1920, 1080
OUT_W, OUT_H = 3840, 2160
FPS = 30

BLOCK = 4
NSYM = 32
RS_N = 255

HEADER_BLOCK = 16
HEADER_NSYM = 64
HEADER_FRAMES = 30
MAGIC = "YTDISK"
VERSION = 2
CRF = 12

# ---------------------------------------------------------------------------
# Optional progress bar
# ---------------------------------------------------------------------------
try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False
    def tqdm(iterable, **kwargs):
        return iterable

# ---------------------------------------------------------------------------
# Capacity
# ---------------------------------------------------------------------------
def capacity(block: int = BLOCK, nsym: int = NSYM, compress_ratio: float = 1.0) -> Dict[str, Any]:
    cols, rows = GRID_W // block, GRID_H // block
    bytes_per_frame = cols * rows / 8
    useful_per_sec = bytes_per_frame * (RS_N - nsym) / RS_N * FPS * compress_ratio
    return {
        "grid": f"{cols}x{rows}",
        "bytes_per_frame": bytes_per_frame,
        "KB_per_frame": bytes_per_frame / 1024,
        "KB_per_sec": useful_per_sec / 1024,
        "MB_per_min": useful_per_sec * 60 / 1e6,
        "minutes_per_GB": 1024 ** 3 / useful_per_sec / 60,
        "compress_ratio": compress_ratio,
    }

# ---------------------------------------------------------------------------
# Bits
# ---------------------------------------------------------------------------
def bytes_to_bits(data: bytes) -> np.ndarray:
    return np.unpackbits(np.frombuffer(data, dtype=np.uint8))

def bits_to_bytes(bits) -> bytes:
    bits = np.asarray(bits, dtype=np.uint8)
    pad = (-len(bits)) % 8
    if pad:
        bits = np.concatenate([bits, np.zeros(pad, dtype=np.uint8)])
    return np.packbits(bits).tobytes()

# ---------------------------------------------------------------------------
# AES-256-GCM
# ---------------------------------------------------------------------------
SALT_LEN, NONCE_LEN = 16, 12

def _derive_key(password: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode())

def encrypt(data: bytes, password: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, nonce = os.urandom(SALT_LEN), os.urandom(NONCE_LEN)
    return salt + nonce + AESGCM(_derive_key(password, salt)).encrypt(nonce, data, None)

def decrypt(blob: bytes, password: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt = blob[:SALT_LEN]
    nonce = blob[SALT_LEN:SALT_LEN + NONCE_LEN]
    ct = blob[SALT_LEN + NONCE_LEN:]
    return AESGCM(_derive_key(password, salt)).decrypt(nonce, ct, None)

# ---------------------------------------------------------------------------
# Reed-Solomon
# ---------------------------------------------------------------------------
def num_codewords(payload_len: int, nsym: int = NSYM) -> int:
    return 0 if nsym == 0 else math.ceil(payload_len / (RS_N - nsym))

def ecc_encode(data: bytes, nsym: int = NSYM) -> bytes:
    if nsym == 0:
        return data
    from reedsolo import RSCodec
    k = RS_N - nsym
    data = data + bytes((-len(data)) % k)
    encoded = bytes(RSCodec(nsym).encode(data))
    codewords = np.frombuffer(encoded, np.uint8).reshape(-1, RS_N)
    return codewords.T.copy().tobytes()  # interleave

def ecc_decode(data: bytes, n_codewords: int, nsym: int = NSYM) -> Tuple[bytes, int]:
    if nsym == 0:
        return data, 0
    from reedsolo import ReedSolomonError, RSCodec
    total = n_codewords * RS_N
    if len(data) < total:
        raise ReedSolomonError("not enough data")
    codewords = np.frombuffer(data[:total], np.uint8).reshape(RS_N, n_codewords).T
    rsc = RSCodec(nsym)
    out = bytearray()
    fixed = 0
    for i, cw in enumerate(codewords):
        try:
            msg, _, errata = rsc.decode(cw.tobytes())
        except ReedSolomonError as e:
            raise ReedSolomonError(f"codeword {i}/{n_codewords} unrecoverable") from e
        fixed += len(errata)
        out += msg
    return bytes(out), fixed

# ---------------------------------------------------------------------------
# Video frames
# ---------------------------------------------------------------------------
def grid(block: int) -> Tuple[int, int]:
    return GRID_W // block, GRID_H // block

def bits_per_frame(block: int = BLOCK) -> int:
    cols, rows = grid(block)
    return cols * rows

def bits_to_frame(bits, block: int = BLOCK) -> np.ndarray:
    cols, rows = grid(block)
    n = cols * rows
    bits = np.asarray(bits, dtype=np.uint8)
    if len(bits) < n:
        bits = np.concatenate([bits, np.zeros(n - len(bits), np.uint8)])
    small = bits[:n].reshape(rows, cols) * 255
    big = small.repeat(block, axis=0).repeat(block, axis=1)
    frame = np.zeros((GRID_H, GRID_W), np.uint8)
    frame[:rows * block, :cols * block] = big
    return frame

def frame_to_bits(frame: np.ndarray, block: int = BLOCK) -> np.ndarray:
    cols, rows = grid(block)
    blocks = frame[:rows * block, :cols * block].astype(np.float32).reshape(rows, block, cols, block)
    m = block // 4
    if m:
        blocks = blocks[:, m:block - m, :, m:block - m]
    return (blocks.mean(axis=(1, 3)) > 127).astype(np.uint8).ravel()

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
_H_CAPACITY = bits_per_frame(HEADER_BLOCK) // 8
_H_CODEWORDS = _H_CAPACITY // RS_N
_H_MAX_DATA = _H_CODEWORDS * (RS_N - HEADER_NSYM)

def make_header_frame(meta: dict) -> np.ndarray:
    from reedsolo import RSCodec
    raw = json.dumps(meta, separators=(",", ":")).encode()
    data = len(raw).to_bytes(2, "big") + raw
    if len(data) > _H_MAX_DATA:
        raise ValueError("header too big (file name too long?)")
    data += bytes(_H_MAX_DATA - len(data))
    return bits_to_frame(bytes_to_bits(bytes(RSCodec(HEADER_NSYM).encode(data))), HEADER_BLOCK)

def read_header_frame(frame: np.ndarray) -> Optional[dict]:
    from reedsolo import ReedSolomonError, RSCodec
    try:
        data = bits_to_bytes(frame_to_bits(frame, HEADER_BLOCK))[:_H_CODEWORDS * RS_N]
        decoded = bytes(RSCodec(HEADER_NSYM).decode(data)[0])
        meta = json.loads(decoded[2:2 + int.from_bytes(decoded[:2], "big")])
    except (ReedSolomonError, ValueError, UnicodeDecodeError, IndexError):
        return None
    return meta if isinstance(meta, dict) and meta.get("magic") == MAGIC else None

# ---------------------------------------------------------------------------
# FFmpeg helpers
# ---------------------------------------------------------------------------
def open_writer(path: str, upscale_4k: bool = True, fps: int = FPS, crf: int = CRF):
    scale = ["-vf", f"scale={OUT_W}:{OUT_H}:flags=neighbor"] if upscale_4k else []
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "gray",
        "-s", f"{GRID_W}x{GRID_H}", "-r", str(fps),
        "-i", "-",
        *scale,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        path,
    ]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE)

def close_writer(proc):
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError("ffmpeg failed while writing the video")

def read_frames(path: str):
    cmd = [
        "ffmpeg", "-loglevel", "error", "-i", path,
        "-vf", f"scale={GRID_W}:{GRID_H}:flags=area",
        "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    size = GRID_W * GRID_H
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield np.frombuffer(buf, np.uint8).reshape(GRID_H, GRID_W)
    finally:
        proc.stdout.close()
        proc.kill()
        proc.wait()

# ---------------------------------------------------------------------------
# Encode / Decode
# ---------------------------------------------------------------------------
def encode_file(
    in_path: str,
    out_path: str,
    block: int = BLOCK,
    nsym: int = NSYM,
    password: Optional[str] = None,
    compress: bool = True,
    upscale: bool = True,
    crf: int = CRF,
    verbose: bool = True,
) -> dict:
    t0 = time.time()
    data = Path(in_path).read_bytes()
    original_size = len(data)
    sha = hashlib.sha256(data).hexdigest()

    if compress:
        data = zlib.compress(data, level=6)
        if verbose:
            print(f"  compressed: {original_size:,} → {len(data):,} bytes "
                  f"({len(data)/original_size*100:.1f}%)")

    payload = encrypt(data, password) if password else data
    ecc = ecc_encode(payload, nsym)
    bits = bytes_to_bits(ecc)
    bpf = bits_per_frame(block)
    n_frames = math.ceil(len(bits) / bpf)

    meta = {
        "magic": MAGIC,
        "v": VERSION,
        "name": Path(in_path).name,
        "size": original_size,
        "sha256": sha,
        "payload_len": len(payload),
        "codewords": num_codewords(len(payload), nsym),
        "block": block,
        "nsym": nsym,
        "frames": n_frames,
        "encrypted": bool(password),
        "compressed": compress,
    }

    writer = open_writer(str(out_path), upscale_4k=upscale, crf=crf)
    header = make_header_frame(meta)
    for _ in range(HEADER_FRAMES):
        writer.stdin.write(header.tobytes())

    frame_iter = range(n_frames)
    if HAS_TQDM and verbose:
        frame_iter = tqdm(frame_iter, desc="encoding frames", unit="frame")

    for i in frame_iter:
        frame = bits_to_frame(bits[i * bpf:(i + 1) * bpf], block)
        writer.stdin.write(frame.tobytes())
        if not HAS_TQDM and verbose and (i % 30 == 0 or i == n_frames - 1):
            print(f"\r  frame {i + 1}/{n_frames}", end="", flush=True)

    close_writer(writer)
    if not HAS_TQDM and verbose:
        print()

    video_size = Path(out_path).stat().st_size
    if verbose:
        print(f" file:  {meta['name']}  ({original_size:,} bytes)")
        print("  NO error correction (nsym=0)" if nsym == 0 else
              f"  with ECC: {len(ecc):,} bytes (+{(len(ecc) / len(payload) - 1) * 100:.0f}%)")
        print(f" video: {out_path}  {OUT_W if upscale else GRID_W}x{OUT_H if upscale else GRID_H}  "
              f"{(HEADER_FRAMES + n_frames) / FPS:.1f}s  {video_size / 1e6:.1f} MB")
        print(f" sha256: {sha[:16]}...    {time.time() - t0:.1f}s")
    return meta

def decode_video(
    video_path: str,
    out_dir: str = ".",
    password: Optional[str] = None,
    verbose: bool = True,
) -> Tuple[bool, Optional[Path]]:
    from cryptography.exceptions import InvalidTag
    from reedsolo import ReedSolomonError

    t0 = time.time()
    frames = read_frames(str(video_path))

    # Find header (try first ~3 seconds)
    meta = None
    header_count = 0
    for i, frame in enumerate(frames):
        m = read_header_frame(frame)
        if m:
            meta = m
            header_count += 1
            if header_count >= 3:  # prefer stable header
                break
        if i > 90:
            break

    if not meta:
        print(" no valid header found – is this a YTDISK video?")
        return False, None

    if verbose:
        print(f" header: {meta['name']}  {meta['size']:,} bytes  "
              f"block={meta['block']}  frames={meta['frames']}  "
              f"encrypted={meta.get('encrypted')}  compressed={meta.get('compressed', False)}")

    # Collect data frames
    chunks = []
    needed = meta["frames"]
    frame_iter = frames
    if HAS_TQDM and verbose:
        frame_iter = tqdm(frames, total=needed, desc="decoding frames", unit="frame")

    for frame in frame_iter:
        if read_header_frame(frame) is not None and len(chunks) == 0:
            continue  # skip remaining headers
        chunks.append(frame_to_bits(frame, meta["block"]))
        if len(chunks) >= needed:
            break
        if not HAS_TQDM and verbose and len(chunks) % 30 == 0:
            print(f"\r  frame {len(chunks)}/{needed}", end="", flush=True)

    if not HAS_TQDM and verbose:
        print()

    if len(chunks) < needed:
        print(f" video too short: got {len(chunks)}/{needed} data frames")
        return False, None

    raw = bits_to_bytes(np.concatenate(chunks))
    try:
        payload, fixed = ecc_decode(raw, meta["codewords"], meta["nsym"])
    except ReedSolomonError as e:
        print(f" Too many errors, Reed-Solomon gave up ({e})")
        print("    try a bigger --block or a higher --nsym, or re-download the 4K stream")
        return False, None

    payload = payload[:meta["payload_len"]]
    if verbose:
        print(f" Reed-Solomon fixed {fixed:,} bytes" if meta["nsym"] else
              "  no error correction")

    if meta.get("encrypted"):
        if not password:
            print(" this file is encrypted – pass --password")
            return False, None
        try:
            payload = decrypt(payload, password)
        except InvalidTag:
            print(" decryption failed: wrong password or corrupted data")
            return False, None

    if meta.get("compressed", False):
        try:
            payload = zlib.decompress(payload)
        except zlib.error:
            print(" decompression failed")
            return False, None

    sha = hashlib.sha256(payload).hexdigest()
    ok = sha == meta["sha256"]
    out = Path(out_dir) / Path(meta["name"]).name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(payload)

    if verbose:
        print(f" expected: {meta['sha256'][:16]}...")
        print(f" got:      {sha[:16]}...")
        print((" IDENTICAL, byte for byte!" if ok else " HASH MISMATCH – file is corrupted")
              + f"  → {out}    {time.time() - t0:.1f}s")
    return ok, out

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(
        prog="ytdisk",
        description="Store files inside 4K YouTube videos (educational)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # encode
    e = sub.add_parser("encode", help="file → 4K video")
    e.add_argument("file")
    e.add_argument("-o", "--output", default="upload.mp4")
    e.add_argument("--block", type=int, default=BLOCK, help="pixel block size (larger = more robust, less capacity)")
    e.add_argument("--nsym", type=int, default=NSYM, help="Reed-Solomon parity bytes (0–127)")
    e.add_argument("--password")
    e.add_argument("--no-compress", action="store_true", help="disable zlib compression")
    e.add_argument("--no-upscale", action="store_true", help="keep 1080p (for testing only)")
    e.add_argument("--crf", type=int, default=CRF, help="libx264 CRF (lower = higher quality)")
    e.add_argument("-q", "--quiet", action="store_true")

    # decode
    d = sub.add_parser("decode", help="video → file")
    d.add_argument("video")
    d.add_argument("-o", "--output", default="restored")
    d.add_argument("--password")
    d.add_argument("-q", "--quiet", action="store_true")

    # capacity
    c = sub.add_parser("capacity", help="show capacity estimates")
    c.add_argument("--block", type=int, default=BLOCK)
    c.add_argument("--nsym", type=int, default=NSYM)
    c.add_argument("--ratio", type=float, default=1.0, help="expected compression ratio (e.g. 0.6)")

    a = p.parse_args()
    verbose = not getattr(a, "quiet", False)

    if a.cmd == "encode":
        encode_file(
            a.file, a.output, a.block, a.nsym, a.password,
            compress=not a.no_compress,
            upscale=not a.no_upscale,
            crf=a.crf,
            verbose=verbose,
        )
    elif a.cmd == "decode":
        ok, _ = decode_video(a.video, a.output, a.password, verbose=verbose)
        sys.exit(0 if ok else 1)
    elif a.cmd == "capacity":
        cap = capacity(a.block, a.nsym, a.ratio)
        print(f"Block size: {a.block}  |  nsym: {a.nsym}  |  grid: {cap['grid']}")
        print(f"Useful data: ~{cap['KB_per_sec']:.1f} KB/s  (~{cap['MB_per_min']:.1f} MB/min)")
        print(f"Time for 1 GB: ~{cap['minutes_per_GB']:.1f} minutes")
        if a.ratio != 1.0:
            print(f"(with compression ratio {a.ratio})")

if __name__ == "__main__":
    main()
