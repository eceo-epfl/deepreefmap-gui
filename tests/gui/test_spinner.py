"""The spinner/stop button itself. Progress bars and the status ticker that this
file used to also cover now live in test_run_progress.py."""

from __future__ import annotations


def test_spinner_timer_tracks_visibility(qapp) -> None:
    from deepreefmap_gui.core.spinner import SpinnerStopButton

    btn = SpinnerStopButton()
    assert not btn._timer.isActive()
    btn.show()
    assert btn._timer.isActive()
    btn.hide()
    assert not btn._timer.isActive()


def test_spinner_stopping_disables_button(qapp) -> None:
    from deepreefmap_gui.core.spinner import SpinnerStopButton

    btn = SpinnerStopButton()
    assert btn.isEnabled()
    btn.set_stopping(True)
    assert not btn.isEnabled()
    assert "Stopping" in btn.toolTip()
    btn.set_stopping(False)
    assert btn.isEnabled()


def test_spinner_emits_clicked(qapp) -> None:
    from deepreefmap_gui.core.spinner import SpinnerStopButton

    fired = []
    btn = SpinnerStopButton()
    btn.clicked.connect(lambda: fired.append(True))
    btn.click()
    assert fired == [True]


def test_spinner_honours_a_larger_size(qapp) -> None:
    from deepreefmap_gui.core.spinner import SpinnerStopButton

    assert SpinnerStopButton(size=40).width() == 40
    assert SpinnerStopButton().width() == 26
