"""Secret/violation scan for the PUBLIC dashboard_app repo (called by hooks/pre-push).
Scans all tracked text files for credentials + paper-boundary violations. Exit 1 on any hit."""
import re
import subprocess
import sys

BLOCK = [
    (re.compile(r"postgres(ql)?://([^\s'\"<>@/]{1,64}):([^\s'\"<>@]{1,128})@", re.I), "credentialed DSN"),
    (re.compile(r"\b(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"), "GitHub PAT"),
    (re.compile(r"KALSHI[_-]?(API[_-]?KEY|PRIVATE)", re.I), "Kalshi credential"),
    (re.compile(r"\b(create_order|place_order|cancel_order)\b"), "order-lifecycle call"),
]
PLACE_U = re.compile(r"^user(name)?$", re.I)
PLACE_P = re.compile(r"^(pass(word)?|pw|secret)$", re.I)
EXTS = (".py", ".yml", ".yaml", ".css", ".md", "Procfile", ".txt", ".sh", "pre-push")


def main() -> int:
    files = subprocess.run(["git", "ls-files"], capture_output=True, text=True).stdout.split()
    bad = 0
    for path in files:
        if not path.endswith(EXTS):
            continue
        if path.startswith("hooks/"):        # the gate's own pattern definitions
            continue
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for rx, why in BLOCK:
                m = rx.search(line)
                if not m:
                    continue
                if why == "credentialed DSN" and PLACE_U.match(m.group(2) or "") \
                        and PLACE_P.match(m.group(3) or ""):
                    continue                  # documentation placeholder user:pass
                print(f"BLOCK {path}:{i} [{why}] {line.strip()[:100]}")
                bad += 1
    print(f"secret_scan: {bad} block(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
