#!/usr/bin/env python3
"""Render this server snapshot for a new IPv4 address; never overwrite output."""
import argparse
import base64
import binascii
import ipaddress
from pathlib import Path
import shutil


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True, help='Public IPv4 address of the new server')
    parser.add_argument('--ssh-host-key', required=True, help='Contents of its ssh_host_ed25519_key.pub')
    parser.add_argument('--output', type=Path, required=True, help='New directory, never /')
    args = parser.parse_args()
    try:
        host = str(ipaddress.IPv4Address(args.host))
        fields = args.ssh_host_key.split()
        if '\n' in args.ssh_host_key or '\r' in args.ssh_host_key or len(fields) not in (2, 3) or fields[0] != 'ssh-ed25519':
            raise ValueError('Expected one ssh-ed25519 public host key')
        decoded = base64.b64decode(fields[1], validate=True)
        if len(decoded) != 51 or not decoded.startswith(b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20'):
            raise ValueError('Invalid Ed25519 public host key')
        key = ' '.join(fields[:2])
    except (ValueError, binascii.Error) as exc:
        parser.error(str(exc))
    if args.output.exists() or args.output.is_symlink():
        parser.error('Output already exists; choose a new directory')
    source = Path(__file__).resolve().parent / 'rootfs'
    shutil.copytree(source, args.output)
    for path in args.output.rglob('*'):
        if path.is_file():
            path.write_text(path.read_text().replace('@@PUBLIC_HOST@@', host).replace('@@SSH_HOST_KEY@@', key))
    print(f'Rendered {args.output}; review before installing. No services were changed.')


if __name__ == '__main__':
    main()
