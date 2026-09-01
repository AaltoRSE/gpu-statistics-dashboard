def pytest_addoption(parser):
    parser.addoption(
        "--update-golden", action="store_true", default=False,
        help="Regenerate tests/golden/*.json from the current API responses "
             "instead of comparing against them.",
    )
