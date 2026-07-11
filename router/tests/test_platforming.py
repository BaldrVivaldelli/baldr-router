from baldr_router.platforming import looks_like_windows_path, windows_path_to_wsl_path


def test_windows_drive_path_fallback() -> None:
    assert (
        windows_path_to_wsl_path("C:\\Users\\me\\project") == "/mnt/c/Users/me/project"
    )


def test_wsl_unc_localhost_path() -> None:
    assert (
        windows_path_to_wsl_path("\\\\wsl.localhost\\Ubuntu\\home\\me\\project")
        == "/home/me/project"
    )


def test_wsl_unc_dollar_path() -> None:
    assert (
        windows_path_to_wsl_path("\\\\wsl$\\Ubuntu\\home\\me\\project")
        == "/home/me/project"
    )


def test_looks_like_windows_path() -> None:
    assert looks_like_windows_path("C:\\Users\\me")
    assert looks_like_windows_path("\\\\wsl.localhost\\Ubuntu\\home\\me")
    assert not looks_like_windows_path("/home/me/project")
