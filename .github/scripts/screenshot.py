"""CI helper: open the app window with sample text and save a screenshot."""
import sys
import tkinter as tk

sys.path.insert(0, ".")
import autotyper
from PIL import ImageGrab

root = tk.Tk()
root.geometry("+40+40")
app = autotyper.App(root)
app.text.insert(
    "1.0",
    "# Status report\n"
    "The **deployment** finished at 02:14 and *nothing* broke.\n"
    "Next: rotate the __access keys__ and ping the ***on-call*** lead.\n\n"
    "- snake_case_name stays as it is, so does 2 * 3 * 4")


def grab():
    root.update()
    ImageGrab.grab().save(sys.argv[1])
    root.destroy()


root.attributes("-topmost", True)
root.after(2500, grab)
root.mainloop()
