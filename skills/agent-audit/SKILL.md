# Audit an agent platform against an outside standard

You are auditing a coding agent or agent platform (hoplon, or another) for security. The list of what to check comes from a published, independent standard, not from the project's own documents. The project's documents are claims to test, and a claim the code doesn't keep is a finding.

## The standards

- **OWASP AISVS 1.0**, the AI Security Verification Standard. It is the checklist: requirements that each begin "Verify that", with a level of 1, 2 or 3. Version 1.0 is frozen, so its identifiers (`v1.0-C9.2.2`) don't move. Get it from the OWASP/AISVS repository on GitHub, folder `1.0/en/`; its `1.0/research/` folder describes how each requirement can be verified. If you can't fetch it, ask the user for a copy. Don't work from memory of it.
- **OWASP Top 10 for Agentic Applications 2026** (ASI01 to ASI10). It is a list of risks, used here as a cross-check: every risk should be covered by something you verified. If you can't fetch it, name risks only from the references in the AISVS chapters, say so, and don't fill in identifiers from memory.
- AISVS covers only what is specific to AI. It leaves general security to other standards: OWASP ASVS for the application, NIST SP 800-190 and the CIS Benchmarks for containers, SLSA for the supply chain. Step 5 covers the parts of those that agent platforms most often get wrong.

## How to run the audit

Work one step at a time, and keep the audit in a file (`audit/LEDGER.md` outside the project, or where the user says) so it can stop and resume, and be split between agents or models. When several models audit the same platform, give them the same level and the same ledger, so their results can be compared item by item.

1. **Scope.** Agree the level with the user (2 unless they say otherwise; it includes level 1), and the chapters (all, unless the user narrows it; record any narrowing at the top of the ledger). Write one paragraph on the system: its parts, what runs where, what each part trusts, and how a request from the model becomes an action. Note the version or commit audited.
2. **Applicability ledger.** List every requirement of AISVS at that level, in every chapter in scope, in order. Mark each one: applies, partly applies, or not applicable, with a one-line reason tied to the system ("no model training", "no vector store", "no MCP client"). Never mark one not applicable because it is hard to check. Appendix C (AI-assisted secure coding) goes in too: a coding agent is the tool it is about, though many of its items are about an organisation's process and are out of scope for the tool.
3. **Evidence.** For each requirement that applies, find what enforces it: the code (`file:line`), the configuration, and what the documents claim. Code that only suggests, or a prompt that asks the model to behave, isn't enforcement: AISVS asks that access decisions be made by the application, never by the model.
4. **Test.** Try to break each one. Write the test or the steps before you run them: in a script, as its docstring, naming the requirement IDs. Where the user interface is a program of its own (a terminal UI, an editor plugin), test the layer its key or click handler calls, cite the handler's line, and say the finding is confirmed at that layer; drive the real interface (in a private terminal multiplexer session) when you can. Record the result: met, partly met, not met, or can't verify (say why).
5. **Beyond AISVS.** Check these too, each against the standard named:
   - Text from the model, files or tools shown in a terminal or UI: escape sequences, hidden or look-alike characters, text that imitates the program's own prompts (ASVS output encoding).
   - Paths: `..`, symlinks, hard links, a leading `-`, files that change between review and use (ASVS file handling).
   - Secrets: in files the model reads, in command lines, logs, error messages, the audit log, and what a sandbox can see (ASVS secrets).
   - Configuration and extensions (plugins, skills, command sets, hooks): who can change them, and whether a change is noticed before it is used.
   - Sandboxes: network, mounts, capabilities, privileges, resource limits, and what survives a run (NIST SP 800-190, CIS).
   - The program's own supply chain: dependencies, how it is installed and updated (SLSA).
6. **Cross-check.** For each ASI01 to ASI10 risk, list the requirements and tests that cover it. A risk nothing covers is a finding, or a gap the report must name.
7. **Report.** Write the ledger and the findings (format below), most severe first, and a short list of what you couldn't verify. If you are running as a subagent and can't write the report file, put the report in your final reply.

## Rules for findings

- **Confirm by running it.** A finding needs a reproduction: a test, a script, or exact steps, and what you saw. Reading code is enough to suspect, not to report. Mark what you couldn't run as unconfirmed. Something missing (no budget, no reviewer) is confirmed by a search of the code, named in the finding, and a test of whatever bound does exist.
- **Every row that isn't met has a finding.** Several partly met rows may share one finding. A requirement that only qualifies a mechanism the system doesn't have is not met, pointing to the same finding, not "not applicable".
- **Run it safely.** Use a throwaway home and configuration directory, a copy of the project, and a fake model server where one will do. Never use the user's real configuration, keys or sessions, and send nothing to a real endpoint unless the user says which.
- **Severity.** High: an action happens that the user didn't approve, or a secret leaves without asking. Medium: an approval is less truthful or complete than it looks, or a boundary holds only by convention. Low: hardening, defence in depth, wrong or missing documentation, or a requirement not met by a documented design choice with a compensating control you tested (name the control and the test).
- **Documentation counts.** Where the documents claim something the code doesn't do, that is a finding, even when the code is the safer one.
- **Say what is good, briefly.** A requirement that is met gets one line of evidence, not praise.

## Formats

The ledger starts with the scope (level, chapters, version or commit) and a summary line: how many requirements are met, partly met, not met, not applicable and can't verify. Then one row per requirement:

| ID | In short | Applies | Status | Evidence | Finding |
| --- | --- | --- | --- | --- | --- |
| v1.0-C9.x.y | (the requirement, in a few words) | yes | partly met | `file:line`; the test that shows it | F3 |

After the rows: a table of tests (script, requirements, result) and a list of what wasn't verified, and why.

Each finding:

```
F3  Medium  v1.0-C9.x.y, ASI0n
What:      <what is wrong, in one sentence>
Reproduce: <steps or test>, and what happened
Expected:  <what the requirement asks>
Fix:       <a suggestion>
Confirmed: yes, <date>, <commit>
```

## For hoplon

Starting points, not conclusions: the program (`hoplon`, and `tui/` for hoplon-tui), `SECURITY.md` (its threat model and earlier findings), `docs/json-protocol.md` (what a front end can send), `docs/sandbox.md`, and `tests/`. `tests/fakeapi.py` is a fake model server you can script, and `tests/test_hoplon.py` has helpers (`make_home`, `Config.set_blessed`) that make a blessed throwaway configuration. Set `XDG_CONFIG_HOME`, `XDG_STATE_HOME` and `HOME` to temporary directories before running hoplon or its tests (the tests run git, so this also keeps your own git configuration out). Audit a `git archive` export of the commit, not the working tree.

To test the sandbox without touching your own containers, give podman its own `HOME`, `XDG_RUNTIME_DIR` and `XDG_DATA_HOME` in the audit folder, and import a tiny image there (`podman import` of a tar of a few host binaries, such as a shell and coreutils); a fresh home has no images, and the sandbox never pulls one. hoplon-tui's approval logic is in `tui/conversation.py` (`Model`, `Card`); its keys are handled in `App.key`.
