"""Run one build command in CI; if it fails, put the end of its output in a
GitHub `::error::` annotation, which can be read without the job log.

    python packaging/windows/ci_run.py <title> -- <command> [args...]
"""
import subprocess
import sys


def main(argv):
    if "--" not in argv:
        print(__doc__)
        return 2
    i = argv.index("--")
    title, cmd = " ".join(argv[:i]) or "command", argv[i + 1:]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, errors="replace")
    lines = []
    for line in p.stdout:
        sys.stdout.write(line)
        lines.append(line.rstrip("\n"))
    rc = p.wait()
    if rc != 0:
        tail = "%0A".join(x.replace("%", "%25").replace("\r", "")
                          for x in lines[-60:])
        print(f"::error title={title} failed (exit {rc})::{tail}", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
