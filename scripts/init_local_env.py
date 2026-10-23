"""Create local demo configuration without reading or replacing an existing .env."""

import secrets
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    target = root / ".env"
    if target.exists():
        print("Existing .env preserved; no configuration was read or changed.")
        return
    tokens = {
        name: secrets.token_urlsafe(36)
        for name in (
            "DEMO_OPERATOR_TOKEN",
            "DEMO_APPROVER_TOKEN",
            "SIMULATOR_READ_TOKEN",
            "SIMULATOR_WRITE_TOKEN",
        )
    }
    lines = root.joinpath(".env.example").read_text(encoding="utf-8").splitlines()
    content = "\n".join(
        key + "=" + tokens[key] if (key := line.split("=", 1)[0]) in tokens else line
        for line in lines
    )
    try:
        with target.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content + "\n")
    except FileExistsError:
        print("Existing .env preserved; no configuration was read or changed.")
        return
    print("Created .env with four random demo tokens. Model configuration remains empty.")


if __name__ == "__main__":
    main()
