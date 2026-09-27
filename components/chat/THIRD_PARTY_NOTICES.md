# Third-party dependency notices

The source and Plugin archives produced by this repository do **not** contain a Python virtual
environment, third-party wheels, or an FFmpeg executable. The public bootstrap calls
`scripts/setup_runtime_tools.sh`, which installs the media packages below into a versioned private
environment on the user's own Mac using the SHA-256 allowlist in
`scripts/requirements-runtime.txt`. `pilk` is a source distribution on macOS, so its build also uses
the hash-pinned `wheel` package from `scripts/requirements-build.txt`.
The older `scripts/setup_content_tools.sh` remains a source-developer helper.
`scripts/setup_key_init_tools.sh`, which belongs only to the explicit source-development setup
workflow, separately installs the pinned Frida and compatibility packages shown here. That
installer accepts only the macOS wheels whose PyPI SHA-256 digests are recorded in
`scripts/requirements-key-init.txt`; it does not allow source-distribution fallback:

| Dependency | Pinned version | Upstream | License note |
| --- | ---: | --- | --- |
| `pilk` | 0.2.4 | <https://github.com/foyoux/pilk> | GPL-3.0 |
| `pycryptodome` | 3.23.0 | <https://www.pycryptodome.org/> | Public-domain and BSD-licensed components; see upstream distribution |
| `zstandard` | 0.23.0 | <https://github.com/indygreg/python-zstandard> | BSD-3-Clause |
| `imageio-ffmpeg` | 0.6.0 | <https://github.com/imageio/imageio-ffmpeg> | Python wrapper is BSD-2-Clause; platform wheels include a separate FFmpeg executable |
| `frida` | 17.16.4 | <https://frida.re/> | wxWindows Library Licence, Version 3.1; source-development key initialization only |
| `typing_extensions` | 4.16.0 | <https://github.com/python/typing_extensions> | PSF-2.0 |
| `wheel` | 0.45.1 | <https://github.com/pypa/wheel> | MIT; build helper only |

The locally inspected macOS `imageio-ffmpeg` wheel contains an FFmpeg 7.1 build configured with
GPL components. Its obligations are not covered by the wrapper's BSD license. Therefore neither
that wheel nor its FFmpeg executable is included in the distributable archives created here.

If a future release bundles any wheel, virtual environment, FFmpeg build, SILK decoder, or signed
Companion containing these components, the release owner must perform a new license review and
ship the applicable complete license texts, component/build BOM, notices, and corresponding-source
materials. This file is an engineering inventory, not legal advice.

## Windows source preview

Windows uses `scripts/requirements-windows.txt`: pywin32 311 (PSF), psutil 7.0.0
(BSD-3-Clause), pysilk-mod 1.6.4 (upstream BSD-style/MIT component notices), and the
same pinned PyCryptodome, zstandard and imageio-ffmpeg versions listed above.
The installer downloads CPython 3.12 x64 wheels from PyPI with verified hashes;
the repository does not redistribute their binaries. pysilk-mod replaces pilk on
Windows only. Its full license is retained inside its upstream wheel:
[license](https://github.com/DCZYewen/Python-Silk-Module/blob/master/LICENSE).
The upstream package contains SILK/pybind components whose notices must be kept
when redistributing. FFmpeg's component licenses remain separate from its wrapper;
commercial rebundling is not approved by this engineering adaptation.

Native Windows code calls Microsoft APIs through pywin32/ctypes; it does not copy
or bundle third-party credential extraction code. The public SQLite lock protocol,
SQLCipher design and MSVC string ABI informed the implementation. See
[review and validation boundaries](docs/WINDOWS_SUPPORT.md).

## Optional video-channel replay Word preview

The separate `wechat-replay-word` bootstrap downloads, but does not redistribute,
mitmproxy 12.2.3 ([MIT](https://github.com/mitmproxy/mitmproxy/blob/main/LICENSE)),
cryptography 48.0.1 ([Apache-2.0 or BSD-3-Clause](https://github.com/pyca/cryptography/blob/main/LICENSE)),
mlx-whisper 0.4.3 ([MIT](https://github.com/ml-explore/mlx-examples/blob/main/LICENSE)),
python-docx 1.2.0 ([MIT](https://github.com/python-openxml/python-docx/blob/master/LICENSE)),
and imageio-ffmpeg 0.6.0 (wrapper and binary licensing described above).
Their transitive dependencies retain their own licenses. This preview pins direct
versions, not a complete transitive hash lock; it must not be advertised as a
reproducible binary release. No certificates, private keys, account catalogs,
session parameters, media, runtime environments or model weights are bundled.

The interception adapter and bounded replay queue are project-authored code using
mitmproxy's public addon interface. They do not incorporate an unreviewed third-party
downloader executable. Model download is a separate explicit step: record its exact
revision, license and file hashes, and never execute custom model-repository code.
Temporary certificate trust is a security-sensitive, separately approved action;
installing the Skill never changes trust or network settings.
