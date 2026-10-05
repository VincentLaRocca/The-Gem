# The Gem

Architecture by Team La Groka.

[![Org](https://img.shields.io/badge/org-lagroka-111111)](https://github.com/lagroka)
[![Repo](https://img.shields.io/badge/repo-The%20Gem-111111)](https://github.com/VincentLaRocca/The-Gem)
[![License](https://img.shields.io/badge/license-TBD-lightgrey)](LICENSE)

A design is not a program. A program that has lost its design cannot be reviewed. The Gem separates the two.

**Team:** Team La Groka · **Slug:** `lagroka` · **Placeholder:** [github.com/VincentLaRocca/The-Gem](https://github.com/VincentLaRocca/The-Gem)

## Clone

```bash
git clone https://github.com/VincentLaRocca/The-Gem.git
```

## Abstract

We propose a system for publishing an architecture without letting the implementation rewrite it. An anchor holds the invariants. A programmer implements against them. Neither role may close the other's work. The repository is the record. What is not in the record is not part of the system.

## 1. Introduction

Commerce on the Internet has come to rely on institutions serving as trusted third parties. Software has come to rely on a similar habit: one author who is both the specification and the patch. When those two disagree, the patch wins, because it runs.

The Gem reverses the order. The design is published first. Code is admitted only if it can be checked against that design. Team La Groka is the institution that keeps the order. The anchor keeps the design. The programmer writes the code.

## 2. Roles

The system has two roles and no third.

**Anchor.** Holds the name, the boundaries, and the decisions that must not move. Does not implement. A change to an invariant is an anchor commit, stated in prose, before any code that depends on it.

**Programmer.** Implements against the current invariants. Does not rename the system, move a boundary, or silently retire a decision. Gemini fills this role. A programmer commit cites the invariant it satisfies.

The anchor for this repository is the design record in this file. The programmer is Gemini. Team La Groka is the name on both.

## 3. The record

The repository is the only ledger.

1. An invariant is a short statement with a stable identifier (`INV-n`).
2. A decision that chooses among invariants is recorded with the alternatives it rejected.
3. A programmer change references the invariant identifiers it implements.
4. A change that contradicts an invariant is not a feature. It is a failed check. It does not land until the anchor amends the invariant in the open.

History is append-only in spirit. Rewrites of the design are new sections, not silent edits to old ones.

## 4. Invariants

- **INV-1.** The organization is Team La Groka. The slug is `lagroka`. Display name and slug are different fields.
- **INV-2.** The repository is The Gem. The intended clone path is `https://github.com/lagroka/The-Gem.git`. Until that org exists, the live path is `https://github.com/VincentLaRocca/The-Gem.git`.
- **INV-3.** The anchor does not implement. The programmer does not rebrand.
- **INV-4.** Display name and path are different fields. Collapsing them is an error.
- **INV-5.** What is not written in this repository is not part of the architecture.

## 5. Boundaries

```text
Team La Groka
└── lagroka/The-Gem
    ├── README.md        anchor record (this file)
    ├── docs/decisions/  rejected alternatives, one file each
    └── src/             programmer surface, cited back to INV-n
```

## 6. Admission

A change is admitted when all of the following hold.

1. It names the role that authored it.
2. If it is code, it cites one or more `INV-n` entries that already exist.
3. If it adds or retires an invariant, the anchor wrote it, and the previous wording remains visible in history.
4. The clone path in this file still names `lagroka/The-Gem` as the intended path.

There is no vote. There is a check. The check is readable by anyone who can read the diff.

## 7. What this is not

The Gem is not a chain, a token, or a consensus protocol. The Satoshi form is used because it fits the problem: a public record, a small set of rules, and no trusted party who may amend both at once. The scarcity here is editorial. An invariant is expensive to change and cheap to cite.

## 8. Conclusion

We have proposed a repository in which the design cannot be outrun by the patch. Team La Groka publishes it. The anchor keeps the invariants. The programmer implements them. The Gem is the record.

## 9. Rebrand

`Team-La-Groka` was a mistaken slug. It is rejected.

The name is Team La Groka. The slug is `lagroka`. Renaming the existing organization is an owner act in GitHub settings. This signature cannot rename an organization, and it does not have admin on `Team-La-Groka`.

- Mistaken org, still live: https://github.com/Team-La-Groka
- Intended org, not yet formed: https://github.com/lagroka
- Live placeholder: https://github.com/VincentLaRocca/The-Gem

## Status

| | |
|---|---|
| Team | Team La Groka |
| Slug | `lagroka` |
| Rejected slug | `Team-La-Groka` |
| Signing account | `VincentLaRocca` |
| Repository | The Gem |
| Live clone path | `https://github.com/VincentLaRocca/The-Gem.git` |
| Intended clone path | `https://github.com/lagroka/The-Gem.git` |
| Anchor | Design record in this file |
| Programmer | Gemini |
| State | Rebrand recorded. Org rename not yet performed. |
