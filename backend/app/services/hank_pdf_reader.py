"""Isolated, resource-bounded native text extraction for untrusted intake PDFs."""

import io
import json
import resource
import sys


def main():
    # Linux production and macOS development both provide resource limits.
    # Apply before importing the PDF parser, and never inherit database work.
    resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    # macOS does not enforce address-space limits consistently. Production is
    # Linux; development still retains the CPU and parent wall-clock limits.
    if sys.platform.startswith('linux'):
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    from pypdf import PdfReader

    content = sys.stdin.buffer.read(10 * 1024 * 1024 + 1)
    if len(content) > 10 * 1024 * 1024 or not content.startswith(b'%PDF-'):
        raise ValueError('PDF input exceeds limits')
    reader = PdfReader(io.BytesIO(content))
    if reader.is_encrypted or not 1 <= len(reader.pages) <= 25:
        raise ValueError('PDF must have 1–25 unencrypted pages')
    print(json.dumps([(page.extract_text() or '')[:16000] for page in reader.pages]))


if __name__ == '__main__':
    main()
