# Third-party notices — TraderInvest FX

TraderInvest FX bundles the following open-source components. Each remains under its own
licence; nothing in the TraderInvest FX licence restricts your rights under these licences.

| Component | Licence | Source |
|---|---|---|
| Python runtime | PSF License 2.0 | https://www.python.org |
| Qt 6 (QtCore, QtGui, QtWidgets, QtNetwork) | GNU LGPL v3 | https://www.qt.io / https://code.qt.io |
| Qt for Python (PySide6, Shiboken6) | GNU LGPL v3 | https://code.qt.io/cgit/pyside/pyside-setup.git |
| NumPy | BSD 3-Clause | https://numpy.org |
| pandas | BSD 3-Clause | https://pandas.pydata.org |
| python-dateutil | Apache 2.0 / BSD 3-Clause | https://github.com/dateutil/dateutil |
| pydantic, pydantic-core, pydantic-settings | MIT | https://github.com/pydantic |
| annotated-types, typing-inspection | MIT | https://github.com/pydantic |
| typing_extensions | PSF License 2.0 | https://github.com/python/typing_extensions |
| tzdata (IANA time zone database) | Apache 2.0 (data: public domain) | https://github.com/python/tzdata |
| SQLAlchemy | MIT | https://www.sqlalchemy.org |
| python-dotenv (via pydantic-settings) | BSD 3-Clause | https://github.com/theskumar/python-dotenv |
| six (via python-dateutil) | MIT | https://github.com/benjaminp/six |
| OpenSSL 3 (libssl, libcrypto) | Apache 2.0 | https://www.openssl.org |
| SQLite | Public domain | https://sqlite.org |
| libffi | MIT | https://sourceware.org/libffi |
| bzip2, xz/liblzma, expat (Python extension modules) | BSD-style / public domain / MIT | https://www.python.org |
| Microsoft Visual C++ runtime (VCRUNTIME140*.dll) | Microsoft Visual C++ Redistributable licence | https://learn.microsoft.com/cpp/windows/redistributing-visual-cpp-files |
| PyInstaller bootloader | GPL v2 with bootloader exception | https://pyinstaller.org |

## LGPL v3 (Qt and PySide6)

Qt and PySide6 are used unmodified and are dynamically linked: their libraries ship as
separate files in the application folder (`_internal/PySide6`) and can be replaced by the user
with compatible versions. The complete corresponding source code of these libraries is
available from the URLs above; on request we provide it for at least three years from
distribution. The full LGPL v3 and GPL v3 texts are available at
https://www.gnu.org/licenses/lgpl-3.0.html and https://www.gnu.org/licenses/gpl-3.0.html.

## Artwork

All graphics (operations room, characters, icon) are generated procedurally by TraderInvest FX
code at runtime. No third-party art assets are included.

## Market data

In SIMULATION mode all prices are synthetic and generated locally. No third-party market data
is redistributed with this package.
