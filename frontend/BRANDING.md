# Branding

The visual direction for the WebbPulse Terraform control plane frontend. It is
this product's own identity, not a copy of the hosted product it replaces: the
information architecture, the run vocabulary and the flows follow that product
so the team does not have to relearn them, and nothing else does.

## Name

The product is **WebbPulse Terraform**. In the interface, "WebbPulse" is set in
the muted weight and "Terraform" in the strong one, so the product reads first
and the owner second. Where the rail is tight, the wordmark drops to
"Terraform" alone. There is no tagline and no slogan anywhere.

## Mark

`src/components/Brand.tsx` holds both the mark and the wordmark.

The mark is a pulse line crossing three stacked layers on a rounded accent
square: the layers are the infrastructure a workspace manages, the line is a
run passing through it. It is drawn from the accent tokens rather than fixed
hex, so it follows the theme without a second file. `public/favicon.svg` is the
same geometry with the dark palette's values baked in, because a favicon has no
document to read tokens from.

Use `Wordmark` in the top bar. Use `BrandMark` where only a square fits.

## Colour

Every colour is a semantic token defined in `src/styles/globals.css`. Nothing
in a component names a hex value or a Tailwind palette colour.

The `@theme` block holds the light values, and a `:root[data-theme='dark']`
block inside `@layer theme` overrides every one of them. Dark is what ships:
`index.html` carries `data-theme="dark"` and only a stored preference of
`light` takes it off, so light is the opt-in even though it is the base layer
of the stylesheet.

### Surfaces and text

| Token                 | Dark      | Light     | Used for                                |
| --------------------- | --------- | --------- | --------------------------------------- |
| `--color-bg`          | `#0f1116` | `#fafafa` | The page behind everything              |
| `--color-panel`       | `#161920` | `#ffffff` | Cards, tables, dialogs, the rail        |
| `--color-raised`      | `#1d212a` | `#f1f2f3` | Hover fills, inset chips                |
| `--color-line`        | `#262b36` | `#e5e6e8` | Ordinary borders and dividers           |
| `--color-line-strong` | `#333946` | `#d5d7db` | Input borders, a button's edge          |
| `--color-text`        | `#c3c9d4` | `#3b3d45` | Body text                               |
| `--color-text-strong` | `#f4f6fa` | `#0c0c0e` | Headings, names, the value that matters |
| `--color-text-muted`  | `#98a0b0` | `#656a76` | Supporting text and labels              |
| `--color-text-faint`  | `#7b8394` | `#737884` | Timestamps, counts, placeholders        |

The dark surfaces run slate rather than neutral grey, so the blue accent and
the diff colours sit on a base that shares their temperature instead of
fighting it.

### Accent

| Token                     | Dark      | Light     |
| ------------------------- | --------- | --------- |
| `--color-accent`          | `#4d9fff` | `#1060ff` |
| `--color-accent-hover`    | `#74b4ff` | `#0c56e9` |
| `--color-accent-soft`     | `#14263d` | `#f2f8ff` |
| `--color-accent-line`     | `#1e3c5f` | `#cce3fe` |
| `--color-accent-contrast` | `#08101c` | `#ffffff` |

The accent is spent on one thing at a time: the primary action, the selected
tab, the focus ring and links. It is a blue, deliberately not the purple the
hosted product uses.

`--color-accent-contrast` is what goes on top of an accent fill. It is not
white, because white on the dark accent is too low in contrast. Any component
filling with `bg-accent` sets `text-accent-contrast`.

### Status

Run states are coloured by tone, not one colour per state, so a new state
inherits its colour by picking a tone. `runTone` in `src/api/runStates.ts` is
the only place the mapping lives.

| State                   | Tone      | Token                              | Reads as              |
| ----------------------- | --------- | ---------------------------------- | --------------------- |
| `pending`               | neutral   | `--color-text` on `--color-raised` | Queued, nothing to do |
| `planning`              | running   | `--color-running`                  | Moving                |
| `planned`               | running   | `--color-running`                  | Moving                |
| `awaiting_confirmation` | attention | `--color-warning`                  | Waiting on a person   |
| `applying`              | running   | `--color-running`                  | Moving                |
| `applied`               | success   | `--color-success`                  | Finished well         |
| `planned_and_finished`  | success   | `--color-success`                  | Finished well         |
| `errored`               | danger    | `--color-danger`                   | Finished badly        |
| `cancelled`             | neutral   | `--color-text` on `--color-raised` | Stopped               |
| `discarded`             | neutral   | `--color-text` on `--color-raised` | Stopped               |

Each tone has a `-soft` fill and a `-line` border to sit on, so a badge is a
pill of soft fill, tone border and tone text. Running states pulse their dot;
terminal states do not.

Amber is reserved for "a person has to act". A run awaiting confirmation is the
only thing in the interface that gets an amber row edge and an amber panel, and
that is what makes it findable in a list.

### Plan diff

The diff colours are separate tokens from the status ones even where a value
matches today, because they answer a different question and should be free to
diverge.

| Action  | Glyph | Token                | Dark      | Light     |
| ------- | ----- | -------------------- | --------- | --------- |
| create  | `+`   | `--color-add`        | `#3fb950` | `#00781e` |
| update  | `~`   | `--color-change`     | `#d29922` | `#9e4b00` |
| delete  | `-`   | `--color-destroy`    | `#f85149` | `#c00005` |
| replace | `-/+` | `--color-replace`    | `#a371f7` | `#6c2bd9` |
| read    | `<=`  | `--color-read`       | `#58a6ff` | `#0c56e9` |
| no-op   | none  | `--color-text-faint` |           |           |

Replace is its own colour rather than a shade of change, because a replacement
destroys and recreates and reading it as an ordinary change understates what
the apply will do. The plan summary counts replacements separately for the same
reason.

Colour never carries the meaning on its own: every row also carries its glyph,
and every glyph is repeated in words for a screen reader.

### Code

`--color-code`, `--color-code-line`, `--color-code-text` and
`--color-code-muted` are a near-black block that stays dark in both themes, so
a log reads the same way whichever theme is on.

## Typography

One family. **Inter** for everything the interface says, and the platform
monospace stack for everything the engine says.

| Role            | Size          | Weight | Colour        |
| --------------- | ------------- | ------ | ------------- |
| Page title      | `text-lg`     | 600    | `text-strong` |
| Section heading | `text-sm`     | 600    | `text-strong` |
| Body            | `text-sm`     | 400    | `text`        |
| Supporting      | `text-xs`     | 400    | `text-muted`  |
| Meta            | `text-xs`     | 400    | `text-faint`  |
| Rail label      | `text-[11px]` | 500    | `text-faint`  |

The scale is deliberately short. Hierarchy comes from weight and colour more
than from size, which keeps a dense table and a run header on the same rhythm.

The rail group label is the one place small caps and letter spacing are used.
It is there because it separates groups of links in a rail without drawing a
line, and it is not a treatment to reach for anywhere else.

Monospace carries identity, never prose: run ids, resource addresses, attribute
names, ARNs, account ids, configuration version ids, engine versions and every
value in a diff. `tnum` is on globally, so columns of counts line up.

Body copy is capped near 70 characters through `max-w-prose`.

## Spacing and radius

Spacing is Tailwind's 4px scale. Inside a card, `px-4 py-3`. Between sections
of a page, `space-y-5` or `space-y-6`. Between a label and its value, `gap-1.5`
or `gap-2`.

Radius says what a thing is, rather than being one value everywhere:

- `rounded-full` for pills and status dots
- `rounded-lg` for panels, cards and lists
- `rounded-md` for inputs, buttons and code blocks
- `rounded` for small inline chips

Borders do the separating, not shadows. `shadow-xs` appears only on a raised
button. A dashed `border-line` is the empty state.

## How components use the tokens

- A **panel** is `border border-line bg-panel rounded-lg`. A list inside it
  divides with `divide-y divide-line`.
- A **badge** is a pill of `<tone>-soft` fill, `<tone>-line` border and `<tone>`
  text.
- A **resource change row** carries a 2px left border in its action's colour,
  its glyph in the same colour in a fixed gutter, then the address in mono.
- A **run needing action** carries a left border in `--color-warning` and a
  faint `warning-soft` wash, in the run list and on the run page's confirmation
  panel.
- **Focus** is a 2px `--color-accent` outline at 2px offset, set once on
  `:focus-visible` in the base layer. Components do not remove it.
- **Motion** is used only to show that something is still moving: the pulsing
  dot on an active badge and the current stage marker. Everything is disabled
  under `prefers-reduced-motion`.

## Copy

- No em dashes, anywhere.
- Say "software engineer", never "developer".
- Sentence case for headings, labels and buttons.
- A button says what happens: "Confirm & apply", "Discard run", "Start run".
  The word survives into the result, so "Discard run" produces a discarded run.
- Errors say what happened and what to do about it. They do not apologise and
  they are never vague.
- An empty state names the next action rather than describing the emptiness.
- Run vocabulary follows the hosted product: plan, apply, confirm, discard,
  cancel, plan only, configuration version, workspace.
- No taglines, no marketing language, no claims about the product.
