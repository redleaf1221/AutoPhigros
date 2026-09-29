import argparse
import hashlib
import sys
from pathlib import Path
from typing import List

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

KEY = "Backward, go backward, turn back to the antemundane realm, go back to the -"

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="c9s_decrypt.py", description="解密Phigros C9S6相关AB"
    )
    parser.add_argument("asset_bundle", type=Path, help="待解密AB")
    parser.add_argument("-o", "--out", type=Path, help="输出目录")
    return parser.parse_args(argv)

def main(argv: List[str] | None = None) -> int:
    args = parse_args(argv)

    if not args.asset_bundle.exists() or args.asset_bundle.is_dir():
        print("文件不存在")
        return 1

    if not args.out:
        args.out = args.asset_bundle.name

    hash_object = hashlib.sha512(KEY.encode())

    key = hash_object.digest()[0:32]
    iv = hash_object.digest()[32:48]

    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()

    ab_raw = Path(args.asset_bundle).read_bytes()
    padded_plaintext = decryptor.update(ab_raw) + decryptor.finalize()
    unpadder = PKCS7(algorithms.AES.block_size).unpadder()
    plaintext = unpadder.update(padded_plaintext) + unpadder.finalize()

    with open(args.out, "wb") as f:
        f.write(plaintext)

    return 0


if __name__ == "__main__":
    sys.exit(main())
