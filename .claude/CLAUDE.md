## no-greeks-here

### Navigation
- Main design doc exists at `@.claude/design/design-doc.md`
- Tracker is the file we update after each claude code session, it checks current status against design doc. It is located at `@.claude/design/tracker.md`
- After each session we will write a summary/ handoff document that will be numbered and have the title of what we just did, I will update this section manually as more documents appear. These must be placed at `@.claude/implementation/`:
- Session handoffs are numbered and live in `.claude/implementation/` . Read the highest numbered one at the start of each session, plus any earlier one relevant to the current task.

### Rules
- When a test fails, report the failure and your diagnosis before changing anything. Never modify a test assertion to make it pass without stating explicitly that you are doing so and why the production behaviour is correct as-is.
- Never read or use any command line tools on `.env`, you may read `.env.example`
- Never add yourself as a contributor in commits.
- Python and the required packages are not available globally instead they are installed in a venv that exists at the project root, you may access it at `venv/Scripts/Activate.ps1`