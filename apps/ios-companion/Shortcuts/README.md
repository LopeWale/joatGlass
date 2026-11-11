# Messages shortcuts for JOAT Brain

Two ready-made Shortcuts automations that let JOAT read and send your texts from the glasses, from your own number. iOS has no other way for an app to read Messages or send a text without a tap, so these are required for Messages on the glasses.

| File | When it runs | What it does |
|---|---|---|
| [Show Messages on Glasses with Contact Names.shortcut](https://github.com/LopeWale/joatGlass/raw/main/apps/ios-companion/Shortcuts/Show%20Messages%20on%20Glasses%20with%20Contact%20Names.shortcut) | A text arrives (any text containing a space) | Hands the text to JOAT Brain with the sender's contact name; a group text arrives as "Group (Person)". JOAT Brain keeps spam from unsaved numbers off the glasses. |
| [Send Message from JOAT Brain.shortcut](https://github.com/LopeWale/joatGlass/raw/main/apps/ios-companion/Shortcuts/Send%20Message%20from%20JOAT%20Brain.shortcut) | JOAT Brain posts "JOAT • text to send" | Sends the text you confirmed on the glasses, to the group when you replied in a group, then tells the glasses it was sent. |

The files here call the repo owner's JOAT Brain (Apple team `J2XU5XWJM9`, bundle `com.joat.brain`). With your own build of JOAT Brain ([docs/IOS_APP.md](../../../docs/IOS_APP.md)), run [one command](#your-own-build) first so they call yours.

## Install (on the iPhone, about 3 minutes)

1. Install your JOAT Brain build and open it once.
2. In Safari on the iPhone, open this page (or your fork's, after [Your own build](#your-own-build)) and tap the first file's link in the table above. Tap **Download**.
3. Tap the download icon in Safari's address bar, then tap the file. Shortcuts opens it; tap the button at the bottom to add it.
4. Do the same for the second file.
5. Open **Shortcuts › Automation**. Both automations are listed there. For each one:
   1. Tap it.
   2. Choose **Run Immediately**.
   3. Turn **Notify When Run** off (otherwise iOS adds its own notification for every text).
   4. Tap **Done**.
6. Test: ask someone to text you. The text shows on the glasses and in JOAT Brain › Messages › Recent. On the glasses, open Messages, tap the conversation, tap to reply, say it, then tap to confirm.

Keep the automations: they are what runs. The shortcuts in the Shortcuts tab are the steps they run.

## Limits

- The text trigger needs a filter, and "contains a space" is the catch-all. One-word texts ("ok") and emoji-only texts don't reach the glasses.
- Nothing is sent without your tap on the glasses.

## Your own build

Your JOAT Brain is signed by your Apple team, with your own bundle ID, so the shortcuts have to point at it. On the Mac you build with, signed in to iCloud:

```sh
cd apps/ios-companion/Shortcuts
python3 make_shortcuts.py --team YOURTEAMID --bundle com.you.joatbrain
```

This rewrites the unsigned shortcuts in `source/` for your app, signs them with Apple's `shortcuts sign --mode anyone`, and replaces the two `.shortcut` files here. AirDrop them to your iPhone (or push them to your fork) and install them as above. The signature names no person (an Apple-issued certificate), so the files are safe to share.

You can also build the same automations by hand: JOAT Brain › Messages has the steps.
