# UI specification (draft, 2026-11-23, updated 2026-11-25)

Status: mockups exist (a private design canvas, "Memoria mockups"); the owner **approved the look on 2026-11-25**,
including the grid home screen and the collection screens. Nothing in this file is built yet.
Companion to [ARCHITECTURE.md](ARCHITECTURE.md). The developer-grade pages under `app/web/` are throwaway.

## Who it is for

The least technical user is a **senior who is not tech literate**. Every screen is judged by whether that person
can use it without help. The audience is the people an owner invites, **family and friends alike**; there is no public use. Copy must never assume
"family" (say "people", "everyone here", "your group chat"). Album names like "Rivera family" are just examples.

## Principles

1. **One clear action per screen.** A big primary button; at most one secondary.
2. **Familiar.** Copy the conventions of Google Photos and Apple Photos (bottom tab bar, photo grid by month,
   search at the top, a bottom sheet for choices). Do not invent new patterns.
3. **Large and calm.** Body text 17-19 px or more, buttons 56-58 px tall, touch targets at least 44 px, contrast at
   least 4.5:1, real buttons and labels (accessible to screen readers).
4. **Follows the phone's light or dark setting.**
5. **Never push.** No notifications, banners or nagging. Questions appear only at the moment they are needed (once,
   skippable). One exception the owner asked for on 2026-11-25: the "Who is this?" pop-up (see below).
6. **Nobody names everyone.** People are offered for naming only when they appear in several photos (see
   `FACE_GROUP_MIN_PHOTOS`).
7. **Phone first**, then iPad and laptop (fluid layouts; not designed yet).
8. It is a **website** (add to the home screen), not a store app.

## Words

- The family space is an **album**. (The database calls it a library.) A curated sub-set inside an album is a
  **collection** (approved 2026-11-25, see "Collections"). Rename the unused `album` table to `collection` before
  building it.
- A person's same-name tiebreaker is a **short description** ("blonde", "cousin"). Other names are **nicknames**.
- The product name is **Memoria** (Latin: memory), chosen 2026-11-23. Availability and trademarks are **not checked**.

## Flow

1. **Sign in / create account**: "What's your name?" (a name only), email and password. (Face ID on
   iPhone is the browser filling a saved password; no biometric data is involved. Faces are never used to sign in.)
   Nicknames are NOT asked here: they belong to an album (family and friends call you different things).
2. **Add a selfie** (account level, skippable): explains that only a numeric face fingerprint is kept, never the photo,
   and that it can be removed any time.
3. **Home: your albums**: a **two-column grid** (chosen over a list), each album with one square cover photo, its
   name and photo count; **Create an album** and **Join with a code** below. Collections never appear here. With one
   album, open it directly.
4. **Join**: tap an invite link (nothing to type), or type a six-character code (`K7M-4PX`, no 0/O/1/I,
   case-insensitive, attempts rate-limited).
5. **What do people here call you?** on joining or creating an album: shows "You'll be known as <name>" (changeable)
   and an optional "Other names" box with examples (Abuela, Mimi, Uncle Joe); says everyone in the album sees them.
   If the album already has someone with that name, ask "Is this you?" with their photos (the selfie, if given, can
   suggest the match); a person who is already claimed cannot be claimed again.
6. **Can this album recognize you?** shown on joining or creating: "Very recommended", what is used (a private number,
   never the selfie), that only this album uses it, that it can be switched off. **Yes** and **Not now** carry equal
   weight; consent is explicit and recorded; nothing is pre-selected.
7. **Invite people** (confirmed 2026-11-25): the **link is the main way** to join and the **code is the backup**;
   who can join as (default: add and organize photos; or only look); expires after 30 days; reset link and code; no
   email address required. **Do this later** skips it.
   - Order when **creating** an album: album name, your name here, recognition consent, invite, then the album.
     Someone **joining** does not get the invite step (the clickable mockup shows it for both).
8. **Photos** (inside an album): a **Collections** row (with "See all"), then **All photos** with a **Select** button,
   the filter chips, and the grid by month; tabs Photos / Search / People / Add.
9. **Filter photos** (a bottom sheet): **People** (tap faces to pick; "Together" = everyone selected is in the photo,
   or "Any of them"), **When** (any time, a year, pick dates, on this day, **no date**: photos with no known date, so
   they can be fixed), **Added by**. The button always says how many photos match ("Show 36 photos"). Planned later:
   places, and "needs a name".
10. **Search**: "Gustavo in 2019" becomes a person chip and a date chip with a count.
11. **Which Maya?**: when a name matches several people, a bottom sheet shows each with faces, short description and
   photo count, plus "Show both".

## Collections

- Different people should see the photos: make a separate **album**. Same people: make a **collection** inside it.
- A collection points to photos and never moves them: everything stays in "All photos", and one photo can be in
  several collections. One level only. No separate invites or permissions.
- Owners and editors create and change collections. An album with no collections shows no Collections row (the way to
  make the first one is Select, then "New collection").
- **Inside a collection:** back to the album, name and photo count, **Add photos**, the grid, and a menu to rename or
  delete (deleting a collection keeps the photos).
- **Two ways to add:** from the collection (**Add photos**, tap pictures, "Add 3 photos"), or from the album
  (**Select**, tap pictures, pick a collection or **New collection**).

## Creating, naming and renaming (mockups drawn 2026-11-25, awaiting review)

- **Create an album:** one box (album name, changeable later) and **Create album**; then the names step, recognition
  consent and invite. The names step must not say "You joined" to the person who just created the album.
- **Is this you?** Photos of the person with the same name; **Yes, that's me** and **No, that's someone else** carry
  equal weight. "No" leads to a required **short description** with a preview ("Maya, cousin").
- **New collection:** a name box and **Create collection**; the selected photos are added.
- **Rename** (confirmed 2026-11-25): only owners rename an album, from Album settings (the API already limits this to
  owners). Owners and editors rename or delete collections, from the "..." button inside them.

## Help, settings and naming (requested 2026-11-25; mockups awaiting review)

- **Help** and **Settings** (gear) icons on Home and on the album screen.
- **Help:** a four-step walkthrough (see photos, find someone, add photos, invite), with Next, Back and Close.
- **Settings** (from Home): name, email, remove selfie and face data, sign out, delete account.
- **Album settings** (from the album): rename (owners), your names here, invite, "Recognize me here" switch (off
  deletes your face data in this album and your name from faces), leave album. Controls a role cannot use are hidden.
- **"Who is this?" pop-up:** when someone unnamed (for example a child without an account) appears in several photos,
  ask for their name. Shown to **owners, editors, and the person who uploaded the photos** (owner, 2026-11-25), when
  they open the album. Appears in at least **5 different photos**; one person at a time, at most one per visit;
  **Not now** means that person is never asked about again (they stay available under People). No notifications.
  - Only owners and editors can upload, so an uploader differs only if later changed to viewer. Such a person does
    **not** get the pop-up and cannot name (owner, 2026-11-25): naming stays owners and editors only.
- **Leaving an album** keeps the photos that person added (confirmed 2026-11-25).

## Error messages (owner left these to Claude)

Say what happened and what to do, next to the thing that failed, with no codes or technical words. Keep what the
person already typed.

| Situation | Message |
|---|---|
| Wrong code | "That code didn't work. Check the letters and try again." |
| Expired or reset invite | "This invite no longer works. Ask for a new link." |
| Too many code attempts | "Too many tries. Wait a few minutes, then try again." |
| Wrong email or password | "That email and password don't match. Try again." |
| Email already has an account | "You already have an account. Sign in instead." |
| No internet | "No internet connection. Check it and try again." |
| Upload failed | "Some photos didn't upload." with **Try again** |
| Selfie unusable | "We couldn't see your face clearly. Face the camera in good light and try again." |
| Same name and description | "Someone here already uses that. Add a different short description." |
| Only owner leaving | "You're the only owner. Make someone else an owner first." |
| Anything else | "Something went wrong. Please try again." |

## Not yet designed

"See all" collections, picking photos after "Select", photo viewer, People tab and the full "Who is this?" naming
flow, upload, managing members, confirmation questions before removing or deleting anything, empty states, the first-time experience of an empty album, iPad and
laptop layouts, and the guardian flow for children.

## Rules for implementation

- Build the real screens from the approved mockups; reuse the API in `app/api/`.
- Invite codes and the account-level selfie need new tables (see ARCHITECTURE.md, Phase 4c) and abuse limits (groups per
  account, storage per album, upload rate).
- Light and dark colours are defined once as variables (see the mockups' `:root` blocks).

## Matching rules (search, type-ahead, codes)

- **Names are matched ignoring capitals, accents and extra spaces**: "maria", "MARIA" and "María" are the same, and
  "jose" finds "José". The stored spelling is always what is displayed. Implemented for the type-ahead
  (`app/services/people.fold`); the same function must be used by search (Phase 5).
- **Invite codes ignore capitals** (and spaces and the dash): `k7m 4px` joins like `K7M-4PX`.
- Emails are compared in lower case.
- Same name plus same short description (compared the same way) cannot exist twice in one album.
