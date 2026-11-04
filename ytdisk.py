

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

#1- config


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
VERSION = 1
CRF = 12



def capacity(block=BLOCK, nsym=NSYM):
    cols, rows = GRID_W // block, GRID_H // block
    bytes_per_frame = cols * rows / 8
    useful_per_sec = bytes_per_frame * (RS_N - nsym) / RS_N * FPS
    return {
        "grid": f"{cols}x{rows}",
        "KB_per_frame": bytes_per_frame / 1024,
        "KB_per_sec": useful_per_sec / 1024,
        "minutes_per_GB": 1024 ** 3 / useful_per_sec / 60,
    }

#2- FILE TO BIT


def bytes_to_bits(data: bytes) -> np.ndarray:
    return np.unpackbits(np.frombuffer(data, dtype=np.uint8))

def bits_to_bytes(bits) -> bytes:
    bits = np.asarray(bits, dtype=np.uint8)
    pad = (-len(bits)) % 8  
    if pad:
        bits = np.concatenate([bits, np.zeros(pad, dtype=np.uint8)])
    return np.packbits(bits).tobytes()

#3- AES 


SALT_LEN, NONCE_LEN = 16, 12


def _derive_key(password: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    return Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(password.encode())


def encrypt(data: bytes, password: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, nonce = os.urandom(SALT_LEN), os.urandom(NONCE_LEN)
    return salt + nonce + AESGCM(_derive_key(password, salt)).encrypt(nonce, data, None)


def decrypt(blob: bytes, password: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, nonce, ct = blob[:SALT_LEN], blob[SALT_LEN:SALT_LEN + NONCE_LEN], blob[SALT_LEN + NONCE_LEN:]
    return AESGCM(_derive_key(password, salt)).decrypt(nonce, ct, None)  


#4- RS


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
    return codewords.T.copy().tobytes()                       


def ecc_decode(data: bytes, n_codewords: int, nsym: int = NSYM):
    if nsym == 0:
        return data, 0
    from reedsolo import ReedSolomonError, RSCodec
    total = n_codewords * RS_N
    if len(data) < total:
        raise ReedSolomonError("not enough data")
    codewords = np.frombuffer(data[:total], np.uint8).reshape(RS_N, n_codewords).T 
    rsc, out, fixed = RSCodec(nsym), bytearray(), 0
    for i, cw in enumerate(codewords):
        try:
            msg, _, errata = rsc.decode(cw.tobytes())
        except ReedSolomonError as e:
            raise ReedSolomonError(f"codeword {i}/{n_codewords} unrecoverable") from e
        fixed += len(errata)
        out += msg
    return bytes(out), fixed

# 5 - VIDEO

def grid(block: int):
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

#6 - HEADER FILES 

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


def read_header_frame(frame: np.ndarray):
    from reedsolo import ReedSolomonError, RSCodec
    data = bits_to_bytes(frame_to_bits(frame, HEADER_BLOCK))[:_H_CODEWORDS * RS_N]
    try:
        decoded = bytes(RSCodec(HEADER_NSYM).decode(data)[0])
        meta = json.loads(decoded[2:2 + int.from_bytes(decoded[:2], "big")])
    except (ReedSolomonError, ValueError, UnicodeDecodeError):
        return None
    return meta if isinstance(meta, dict) and meta.get("magic") == MAGIC else None

#7 - video export

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
    cmd = ["ffmpeg", "-loglevel", "error", "-i", path,
           "-vf", f"scale={GRID_W}:{GRID_H}:flags=area",
           "-f", "rawvideo", "-pix_fmt", "gray", "-"]
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

#8- encoder

def encode_file(in_path, out_path, block=BLOCK, nsym=NSYM, password=None):
    t0 = time.time()
    data = Path(in_path).read_bytes()
    sha = hashlib.sha256(data).hexdigest()           

    payload = encrypt(data, password) if password else data
    ecc = ecc_encode(payload, nsym)
    bits = bytes_to_bits(ecc)
    bpf = bits_per_frame(block)
    n_frames = math.ceil(len(bits) / bpf)

    meta = {
        "magic": MAGIC, "v": VERSION, "name": Path(in_path).name, "size": len(data),
        "sha256": sha, "payload_len": len(payload),
        "codewords": num_codewords(len(payload), nsym),
        "block": block, "nsym": nsym, "frames": n_frames, "encrypted": bool(password),
    }

    writer = open_writer(str(out_path))
    header = make_header_frame(meta)
    for _ in range(HEADER_FRAMES):
        writer.stdin.write(header.tobytes())
    for i in range(n_frames):
        writer.stdin.write(bits_to_frame(bits[i * bpf:(i + 1) * bpf], block).tobytes())
        if i % 30 == 0 or i == n_frames - 1:
            print(f"\r  frame {i + 1}/{n_frames}", end="", flush=True)
    close_writer(writer)
    print()

    video_size = Path(out_path).stat().st_size
    print(f" file:  {meta['name']}  ({len(data):,} bytes)")
    print("  NO error correction (nsym=0)" if nsym == 0 else
          f"  with ECC: {len(ecc):,} bytes (+{(len(ecc) / len(payload) - 1) * 100:.0f}%)")
    print(f" video: {out_path}  {OUT_W}x{OUT_H}  {(HEADER_FRAMES + n_frames) / FPS:.1f}s  "
          f"{video_size / 1e6:.1f} MB")
    print(f" sha256: {sha[:16]}...    {time.time() - t0:.1f}s")
    return meta

#9- decoder

def decode_video(video_path, out_dir=".", password=None):
    from cryptography.exceptions import InvalidTag
    from reedsolo import ReedSolomonError

    t0 = time.time()
    frames = read_frames(str(video_path))

    meta = None
    for i, frame in enumerate(frames):
        meta = read_header_frame(frame)
        if meta or i > 90:
            break
    if not meta:
        print(" no valid header found, is this a YTDISK video?")
        return False, None
    print(f" header: {meta['name']}  {meta['size']:,} bytes  block={meta['block']}  "
          f"frames={meta['frames']}  encrypted={meta['encrypted']}")

    chunks = []
    for frame in frames:
        if read_header_frame(frame) is None:
            chunks.append(frame_to_bits(frame, meta["block"]))
            break

    for frame in frames:
        if len(chunks) >= meta["frames"]:
            break
        chunks.append(frame_to_bits(frame, meta["block"]))
        if len(chunks) % 30 == 0:
            print(f"\r  frame {len(chunks)}/{meta['frames']}", end="", flush=True)
    print()
    if len(chunks) < meta["frames"]:
        print(f" video too short: {len(chunks)}/{meta['frames']} data frames")
        return False, None

    raw = bits_to_bytes(np.concatenate(chunks))
    try:
        payload, fixed = ecc_decode(raw, meta["codewords"], meta["nsym"])
    except ReedSolomonError as e:
        print(f" Too many errors, Reed-Solomon gave up ({e})")
        print("    try a bigger --block or a higher --nsym")
        return False, None
    payload = payload[:meta["payload_len"]]
    print(f" Reed-Solomon fixed {fixed:,} bytes" if meta["nsym"] else
          "  no error correction, whatever arrived is what you get")

    if meta["encrypted"]:
        if not password:
            print(" this file is encrypted, pass --password")
            return False, None
        try:
            payload = decrypt(payload, password)
        except InvalidTag:
            print(" decryption failed: wrong password, or the data got corrupted")
            return False, None

    sha = hashlib.sha256(payload).hexdigest()
    ok = sha == meta["sha256"]
    out = Path(out_dir) / Path(meta["name"]).name      
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(payload)
    print(f" expected: {meta['sha256'][:16]}...")
    print(f" got:      {sha[:16]}...")
    print((" IDENTICAL, byte for byte!" if ok else " HASH MISMATCH, file is corrupted")
          + f"  → {out}    {time.time() - t0:.1f}s")
    return ok, out

#10 - main

def main():
    p = argparse.ArgumentParser(prog="ytdisk", description="Store files inside 4K YouTube videos")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("encode", help="file -> 4K video")
    e.add_argument("file")
    e.add_argument("-o", "--output", default="upload.mp4")
    e.add_argument("--block", type=int, default=BLOCK)
    e.add_argument("--nsym", type=int, default=NSYM)
    e.add_argument("--password")

    d = sub.add_parser("decode", help="video -> file")
    d.add_argument("video")
    d.add_argument("-o", "--output", default="restored")
    d.add_argument("--password")

    a = p.parse_args()
    if a.cmd == "encode":
        encode_file(a.file, a.output, a.block, a.nsym, a.password)
    else:
        ok, _ = decode_video(a.video, a.output, a.password)
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()