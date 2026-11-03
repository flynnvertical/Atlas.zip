"""Lanzador del sandbox de run_python: aplica Landlock y luego ejecuta el script del agente.

Se ejecuta como archivo independiente (python -I sandbox_boot.py ...) DESPUÉS de unshare/sudo/setpriv,
porque Landlock exige no_new_privs, que impediría usar sudo si se aplicara antes.

Política (lista blanca; todo lo demás queda denegado):
  - lectura + ejecución: sistema (/usr, /lib*, /bin, /sbin, /etc) y el intérprete con sus paquetes;
  - lectura + escritura: /dev (null, urandom, tty) y el espacio de trabajo del trabajo;
  - sólo lectura: la carpeta de archivos compartidos;
  - SIN acceso: base de datos del agente, /proc (entorno de otros procesos), /tmp, el resto del disco;
  - sin TCP (Landlock red, ABI >= 4) y sin señales a procesos fuera del sandbox (ABI >= 6).
Uso: sandbox_boot.py <workspace> <compartido|-> <script.py>
Sale con 97 si no puede aplicar el aislamiento (nunca ejecuta el script sin aislar).
"""
import ctypes
import os
import site
import sys

SYS_CREATE, SYS_ADD_RULE, SYS_RESTRICT = 444, 445, 446  # iguales en x86_64 y aarch64
RULE_PATH_BENEATH = 1

EXECUTE, WRITE_FILE, READ_FILE, READ_DIR = 1 << 0, 1 << 1, 1 << 2, 1 << 3
REMOVE_DIR, REMOVE_FILE, MAKE_CHAR, MAKE_DIR = 1 << 4, 1 << 5, 1 << 6, 1 << 7
MAKE_REG, MAKE_SOCK, MAKE_FIFO, MAKE_BLOCK, MAKE_SYM = 1 << 8, 1 << 9, 1 << 10, 1 << 11, 1 << 12
REFER, TRUNCATE, IOCTL_DEV = 1 << 13, 1 << 14, 1 << 15
FILE_RIGHTS = EXECUTE | WRITE_FILE | READ_FILE | TRUNCATE | IOCTL_DEV


class PathBeneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def _fail(msg: str) -> None:
    sys.stderr.write(f"SANDBOX_FS_NO_DISPONIBLE: {msg}\n")
    sys.exit(97)


def restrict(workspace: str, shared: str | None) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    abi = libc.syscall(SYS_CREATE, None, ctypes.c_size_t(0), ctypes.c_uint32(1))
    if abi < 1:
        _fail(f"Landlock no soportado (errno {ctypes.get_errno()})")
    handled = (1 << 13) - 1
    if abi >= 2:
        handled |= REFER
    if abi >= 3:
        handled |= TRUNCATE
    if abi >= 5:
        handled |= IOCTL_DEV
    fields = [handled]
    if abi >= 4:
        fields.append(1 | 2)  # BIND_TCP | CONNECT_TCP: sin reglas => TCP denegado
    if abi >= 6:
        fields.append(1 | 2)  # SCOPE_ABSTRACT_UNIX_SOCKET | SCOPE_SIGNAL
    attr = (ctypes.c_uint64 * len(fields))(*fields)
    rs = libc.syscall(SYS_CREATE, ctypes.byref(attr), ctypes.c_size_t(ctypes.sizeof(attr)), ctypes.c_uint32(0))
    if rs < 0:
        _fail(f"landlock_create_ruleset errno {ctypes.get_errno()}")

    read_x = EXECUTE | READ_FILE | READ_DIR
    rules: list[tuple[str, int]] = []
    roots = ["/usr", "/lib", "/lib64", "/lib32", "/bin", "/sbin", "/etc",
             sys.prefix, sys.exec_prefix, sys.base_prefix, os.path.dirname(os.path.realpath(sys.executable))]
    try:
        roots += site.getsitepackages() + [site.getusersitepackages()]
    except Exception:  # noqa: BLE001
        pass
    rules += [(p, read_x) for p in roots]
    rules.append(("/dev", READ_FILE | WRITE_FILE | READ_DIR | (IOCTL_DEV if abi >= 5 else 0)))
    if shared:
        rules.append((shared, READ_FILE | READ_DIR))
    rules.append((workspace, handled))  # control total sólo dentro del espacio del trabajo

    seen = set()
    for path, access in rules:
        if not path or not os.path.exists(path):
            continue
        real = os.path.realpath(path)
        if (real, access) in seen:
            continue
        seen.add((real, access))
        fd = os.open(real, os.O_PATH | os.O_CLOEXEC)
        try:
            if not os.path.isdir(real):
                access &= FILE_RIGHTS
            pb = PathBeneath(access & handled, fd)
            if libc.syscall(SYS_ADD_RULE, ctypes.c_int(rs), ctypes.c_int(RULE_PATH_BENEATH),
                            ctypes.byref(pb), ctypes.c_uint32(0)) < 0:
                _fail(f"landlock_add_rule({real}) errno {ctypes.get_errno()}")
        finally:
            os.close(fd)
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        _fail("no se pudo activar no_new_privs")
    if libc.syscall(SYS_RESTRICT, ctypes.c_int(rs), ctypes.c_uint32(0)) < 0:
        _fail(f"landlock_restrict_self errno {ctypes.get_errno()}")
    os.close(rs)
    return abi


def main() -> None:
    if len(sys.argv) < 4:
        _fail("uso: sandbox_boot.py <workspace> <compartido|-> <script> [args...]")
    workspace, shared, script, extra = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
    workspace = os.path.realpath(workspace)
    tmp = os.path.join(workspace, ".tmp")
    os.makedirs(tmp, exist_ok=True)
    os.environ["TMPDIR"] = tmp
    os.chdir(workspace)
    restrict(workspace, None if shared == "-" else os.path.realpath(shared))
    import runpy
    sys.argv = [script] + extra
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()
