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

## Lock onto the text box

Tick "Lock onto the text box" to keep the typing in the box that has the
cursor when typing begins:

- Mouse clicks are ignored everywhere until typing finishes, so a stray click
  can't move the cursor to another box. The pointer still moves.
- If the window changes anyway (Alt+Tab, a pop-up), typing pauses and carries
  on from the same place when you return to it.
- The Stop button can't be clicked while locked: press F6 to stop.

## Notes

- The target should be a plain text field. Autocorrect, auto-indent or
  auto-closing brackets will throw the corrections off; unticking "Revise
  with arrow keys and Delete" limits fixes to Backspace.
- The speed setting is raw keying speed; pauses and corrections make the
  overall speed lower.
- Symbols assume a US keyboard layout unless that option is unticked.
- macOS needs Accessibility permission for the terminal. On Linux it works
  under X11, not reliably under Wayland.
