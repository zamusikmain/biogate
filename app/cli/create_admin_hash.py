import getpass
import sys

from argon2 import PasswordHasher, Type


def main() -> int:
    password = getpass.getpass("Введите пароль: ")
    repeated = getpass.getpass("Повторите пароль: ")
    if not password:
        print("Пароль не может быть пустым", file=sys.stderr)
        return 1
    if password != repeated:
        print("Пароли не совпадают", file=sys.stderr)
        return 1
    password_hash = PasswordHasher(type=Type.ID).hash(password)
    del password, repeated
    print(password_hash)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
