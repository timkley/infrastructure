"""Install a checksum-pinned official wacli release during the image build."""

import hashlib
import io
from pathlib import Path, PurePosixPath
import sys
import tarfile
import urllib.request


VERSION = "0.20.0"
CHECKSUMS = {
    "amd64": "f243ea7c70f7ff8fc4de7fd7eb478698708acbd8470ad38ffc13379806321e8c",
    "arm64": "fffcb19601b72a4cd3edfb8560fecc6f850b9be5a803e2b9a69a100f59595831",
}


def main() -> None:
    arch = sys.argv[1]
    if arch not in CHECKSUMS:
        raise SystemExit(f"Unsupported architecture: {arch}; use amd64 or arm64")
    filename = f"wacli_{VERSION}_linux_{arch}.tar.gz"
    url = f"https://github.com/openclaw/wacli/releases/download/v{VERSION}/{filename}"
    with urllib.request.urlopen(url, timeout=90) as response:
        payload = response.read()
    if hashlib.sha256(payload).hexdigest() != CHECKSUMS[arch]:
        raise SystemExit("wacli release checksum mismatch")
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        candidates = [
            member for member in archive.getmembers()
            if member.isfile() and PurePosixPath(member.name).name == "wacli"
        ]
        if len(candidates) != 1:
            raise SystemExit("Expected exactly one wacli binary in the release")
        binary = archive.extractfile(candidates[0])
        if binary is None:
            raise SystemExit("Cannot read the wacli release binary")
        destination = Path("/usr/local/bin/wacli")
        destination.write_bytes(binary.read())
        destination.chmod(0o755)
    print(f"Installed verified wacli {VERSION} for {arch}")


if __name__ == "__main__":
    main()
