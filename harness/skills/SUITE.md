# BOOST_AI skill suite

Curated third-party skills shipped inside BOOST_AI, pinned to the commits below so they never
change underneath you. Every runtime (Claude Code, DeepSeek, Codex) sees them. Like any skill,
they are loaded on demand: prompts carry only name + description, and when a turn is planned,
only the skills the approved plan names are offered to the implementation.

They live in BOOST_AI's own layer, split by category (the Area column):
`harness/skills/<category>/<skill>/`. A project skill (`.boost-ai/skills/<category>/<skill>/`) with
the same name overrides one here. Other agents' folders (`~/.claude/skills`, `~/.agents/skills`,
`~/.codex/skills`) are never read.

Updating a skill is a deliberate change: copy the new version from upstream, update its commit
here, review the diff, run the tests.

| Area | Skill | Source | Commit | License |
|---|---|---|---|---|
| Architecture | codebase-design | mattpocock/skills `skills/engineering/codebase-design` | b0618bc | MIT |
| Architecture | improve-codebase-architecture | mattpocock/skills `skills/engineering/improve-codebase-architecture` | b0618bc | MIT |
| Architecture | domain-modeling | mattpocock/skills `skills/engineering/domain-modeling` | b0618bc | MIT |
| Frontend | frontend-design | anthropics/skills `skills/frontend-design` | 683bc88 | Apache-2.0 (LICENSE.txt inside) |
| Frontend | vercel-react-best-practices | vercel-labs/agent-skills `skills/react-best-practices` | 063bee9 | MIT |
| Frontend | vercel-composition-patterns | vercel-labs/agent-skills `skills/composition-patterns` | 063bee9 | MIT |
| Frontend | web-design-guidelines | vercel-labs/agent-skills `skills/web-design-guidelines` + vercel-labs/web-interface-guidelines `command.md` | 063bee9 + 434b7f9 | MIT |
| Security | differential-review | trailofbits/skills `plugins/differential-review` | 82fe822 | CC BY-SA 4.0 |
| Security | sharp-edges | trailofbits/skills `plugins/sharp-edges` | 82fe822 | CC BY-SA 4.0 |
| Security | supply-chain-risk-auditor | trailofbits/skills `plugins/supply-chain-risk-auditor` | 82fe822 | CC BY-SA 4.0 |
| QA | tdd | mattpocock/skills `skills/engineering/tdd` | b0618bc | MIT |
| QA | verification-before-completion | obra/superpowers `skills/verification-before-completion` | 8ca22db | MIT |
| QA | property-based-testing | trailofbits/skills `plugins/property-based-testing` | 82fe822 | CC BY-SA 4.0 |
| QA | webapp-testing | anthropics/skills `skills/webapp-testing` | 683bc88 | Apache-2.0 (LICENSE.txt inside) |
| Interviewing | grilling | mattpocock/skills `skills/productivity/grilling` | b0618bc | MIT |
| Debugging | systematic-debugging | obra/superpowers `skills/systematic-debugging` | 8ca22db | MIT |
| Code review | code-review | mattpocock/skills `skills/engineering/code-review` | b0618bc | MIT |
| Code review | receiving-code-review | obra/superpowers `skills/receiving-code-review` | 8ca22db | MIT |
| Project setup | claude-automation-recommender (Claude Code Setup) | anthropics/claude-plugins-official `plugins/claude-code-setup` | f713a7c | Apache-2.0 (LICENSE inside) |
| Project setup | claude-md-improver | anthropics/claude-plugins-official `plugins/claude-md-management` | f713a7c | Apache-2.0 (LICENSE inside) |
| Building with AI | claude-api | anthropics/skills `skills/claude-api` | 683bc88 | Apache-2.0 (LICENSE.txt inside) |
| Building with AI | mcp-builder | anthropics/skills `skills/mcp-builder` | 683bc88 | Apache-2.0 (LICENSE.txt inside) |
| Building with AI | skill-creator | anthropics/skills `skills/skill-creator` | 683bc88 | Apache-2.0 (LICENSE.txt inside) |

Full upstream commits: anthropics/skills 683bc88e56f3e09ba94f7055977f3d3aa499f202 ·
mattpocock/skills b0618bc436ad893b3c5e84e55fba86586d34a404 · obra/superpowers
8ca22dba9a94f28898bbce59f2537ff4d87c747d · trailofbits/skills 82fe8226252622fa807643bdca1710901198553a ·
vercel-labs/agent-skills 063bee94c3f4df8453406c830b0a7df0f2860278 ·
vercel-labs/web-interface-guidelines 434b7f91364665f2f733b310ec54809bf8f37937 ·
anthropics/claude-plugins-official f713a7c59b729741282f9c2d9a04e28e2abbd20c.

## Licenses and attribution

- MIT texts: `licenses/mattpocock-skills.LICENSE`, `licenses/obra-superpowers.LICENSE`,
  `licenses/vercel-web-interface-guidelines.LICENSE` (vercel-labs/agent-skills declares MIT in its
  README and in each skill's frontmatter).
- Apache-2.0: the `LICENSE.txt` / `LICENSE` shipped inside each Anthropic skill (`frontend-design/`,
  `webapp-testing/`, `claude-api/`, `mcp-builder/`, `skill-creator/`, `claude-automation-recommender/`,
  `claude-md-improver/`; the last two copied from their plugin folder).
- CC BY-SA 4.0: `licenses/trailofbits-skills.LICENSE`. The three Trail of Bits skills are © Trail of
  Bits, used unmodified, and remain under CC BY-SA 4.0 (that license does not extend to BOOST_AI).

## Modifications

- `web-design-guidelines/SKILL.md`: reads the pinned `guidelines.md` (a copy of
  vercel-labs/web-interface-guidelines `command.md` at 434b7f9) instead of fetching the latest
  version from GitHub on every run.
- Upstream `.zip` bundles were not copied. Nothing else was changed.

## Worth knowing

- `skill-creator` evaluates skills by running `claude -p` many times (it uses your Claude quota), and
  its review viewer stops whatever process listens on port 3117 before it starts.
- `supply-chain-risk-auditor` queries public advisory APIs (OSV, npm, PyPI, deps.dev, GitHub) and
  sends your `gh` token only to api.github.com.

## Not included, and why

- Anthropic's docx / pdf / xlsx / pptx skills: their license forbids copying or redistribution.
- Anthropic's `claude-security` plugin: proprietary; it may not be redistributed or used with
  non-Anthropic products (BOOST_AI also runs DeepSeek and Codex). Install it in Claude Code itself.
- Anthropic's `doc-coauthoring`: ships without a license file.
- Skills that duplicate what the harness owns (planning, plan execution, worktrees, commits,
  branches/PRs): superpowers `writing-plans`, `executing-plans`, `using-git-worktrees`,
  `finishing-a-development-branch`; mattpocock `pr`, `to-spec` (the harness's `/interview` writes
  specs).
- trailofbits `second-opinion` (the harness already runs an independent cross-model review),
  `semgrep` (built on interactive tools a headless run does not have), `entry-point-analyzer`
  (smart contracts only), `mutation-testing` (needs Trail of Bits' own mutation tools),
  `diagramming-code` (needs trailmark).
