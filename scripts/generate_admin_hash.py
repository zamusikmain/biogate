"""Generate an Argon2id hash without exposing the password in shell history."""

from getpass import getpass

from argon2 import PasswordHasher, Type


def main() -> None:
    password = getpass("Password: ")
    confirmation = getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("Passwords do not match.")
    if (
        len(password) < 12
        or not any(character.isalpha() for character in password)
        or not any(character.isdigit() for character in password)
    ):
        raise SystemExit("Password must be at least 12 characters and contain letters and digits.")
    print(PasswordHasher(type=Type.ID).hash(password))


if __name__ == "__main__":
    main()
