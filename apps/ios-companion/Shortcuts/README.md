# Messages shortcuts for JOAT Brain

Two Shortcuts automations let JOAT read and send your texts from the glasses, from your own number. iOS has no other way for an app to read Messages or send a text without a tap, so they are required for Messages on the glasses.

| File | When it runs | What it does |
|---|---|---|
| [Show Texts on Glasses.shortcut](https://github.com/LopeWale/joatGlass/raw/main/apps/ios-companion/Shortcuts/Show%20Texts%20on%20Glasses.shortcut) | A text arrives (any text with a space in it) | Three actions: Find Conversations (the newest), JOAT Brain's Show Text on Glasses (the sender, the text, the group's name), Mark as Unread. JOAT Brain finds the sender's name in Contacts, shows a group text as "Group (Person)", and keeps spam from unsaved numbers off the glasses. |
| [Send Message from JOAT Brain.shortcut](https://github.com/LopeWale/joatGlass/raw/main/apps/ios-companion/Shortcuts/Send%20Message%20from%20JOAT%20Brain.shortcut) | JOAT Brain posts "JOAT • text to send" | Sends the text you confirmed on the glasses, to the group when you replied in a group, then tells the glasses it was sent. |

Since iOS 27 a shortcut carries its automation, so adding one adds its automation too.

## Install (on the iPhone, about 2 minutes)

1. Install JOAT Brain from TestFlight and open it once.
2. JOAT Brain › Messages › **Add “Show Texts on Glasses” to Shortcuts**. Shortcuts opens; tap **Add Shortcut**.
3. The same for **Add “Send Message from JOAT Brain” to Shortcuts**.
4. For each of the two, in Shortcuts: tap it, tap **Edit**, then the Info button (ⓘ), tap **Privacy**, and turn on **Allow Running When Locked** (Apple's guide: [Add automations](https://support.apple.com/guide/shortcuts/add-automations-apdfbdbd7123/ios)).
5. If Shortcuts asks to allow an action (Messages, JOAT Brain), choose **Always Allow**.
6. If you had the older **Show Messages on Glasses with Contact Names**, delete it, or texts come through it as well.
7. Test: ask someone to text you. The text shows on the glasses and in JOAT Brain › Messages › Recent. On the glasses, open Messages, tap the conversation, tap to reply, say it, then tap to confirm.

The buttons open iCloud links to the two shortcuts, for the repo owner's JOAT Brain (Apple team `J2XU5XWJM9`, bundle `com.joat.brain`). With your own build, use the files instead ([Your own build](#your-own-build)).

## Limits

- The text trigger needs a filter, and "contains a space" is the catch-all. One-word texts ("ok") and emoji-only texts don't reach the glasses.
- Nothing is sent without your tap on the glasses.

## Your own build

Your JOAT Brain is signed by your Apple team, with your own bundle ID, so the shortcuts have to point at it. On the Mac you build with, signed in to iCloud:

```sh
cd apps/ios-companion/Shortcuts
python3 make_shortcuts.py --team YOURTEAMID --bundle com.you.joatbrain
```

This rewrites the unsigned shortcuts in `source/` for your app, signs them with Apple's `shortcuts sign --mode anyone`, and replaces the two `.shortcut` files here. AirDrop them to your iPhone (or open them in Files) and tap Add Shortcut, then do steps 4 to 6 above. The signature names no person (an Apple-issued certificate), so the files are safe to share. To give your own build's Messages page one-tap buttons, open each file in Shortcuts on your Mac, then Share › Copy iCloud Link, and put the links in `MessagesView.showLink` and `sendLink` (`AppScreens.swift`).
