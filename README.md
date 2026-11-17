# Let us type

An auto typer that types text into whatever window has focus, with a human
rhythm: varied key holds and gaps, overlapping keys, bursts, pauses, typos
that get corrected, and later revisions. The finished text always matches
what you entered.

## Run

```bash
pip install pynput
```

```bash
python autotyper.py
```

Enter the text, press Start (or F6), and click into the target window before
the countdown ends. F6 again stops it.

## Notes

- The target should be a plain text field. Autocorrect, auto-indent or
  auto-closing brackets will throw the corrections off; unticking "Revise
  with arrow keys and Delete" limits fixes to Backspace.
- The speed setting is raw keying speed; pauses and corrections make the
  overall speed lower.
- Symbols assume a US keyboard layout unless that option is unticked.
- macOS needs Accessibility permission for the terminal. On Linux it works
  under X11, not reliably under Wayland.
