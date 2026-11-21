# Help the user write a genai profile (a persona)

You are helping the user create a profile for genai, the command-line client they are using right now. A profile is a persona: it names an endpoint, gives the model standing instructions, picks default skills and sampling settings, and can turn on coding mode. Where prompts are sent is decided by the endpoint, not the profile; the `genai-endpoint` skill helps with those.

## What a profile is

One JSON file:

```json
{
  "name": "coder",
  "description": "Careful senior engineer: small diffs, tests first",
  "endpoint": "work-llm",
  "persona": "You are a careful senior software engineer. Prefer small, reviewable changes. Read the code before proposing edits. Say what you would test and how.",
  "skills": ["bash-scripts"],
  "params": {"temperature": 0.2},
  "coding": true
}
```

| Field | Required | Meaning |
| --- | --- | --- |
| `name` | yes | Used with `genai -p NAME`. Letters, digits, `.`, `_`, `-`. Match the file name (`NAME.json`). |
| `description` | no | One line, shown in `genai --list-profiles`, the REPL banner and when blessing. |
| `endpoint` | no | Name of a blessed endpoint. `-e` on the command line overrides it; without either, `default_endpoint` in `config.json` is used. |
| `persona` | no | Instructions sent to the model with every request, after genai's own rules. It shapes voice and focus; it cannot change genai's rules (the model still only proposes). |
| `skills` | no | Skill names used by default with this profile. Leave it out to use `default_skills` from `config.json`; `[]` means none. `-k` adds more. |
| `params` | no | Request fields such as `temperature`, layered over the endpoint's. May not set `model`, `messages` or `tools`. |
| `coding` | no | `true` turns coding mode on: the model reads and lists files in the working directory without asking. Writes still ask. It is still refused in the home directory and `/`. |

Unknown fields are an error, so don't invent any. Connection details (`base_url`, keys, headers) never go in a profile.

## How to write a good persona

- Ask what the user wants this persona for, one question at a time: the kind of work, the tone (terse or explanatory), what it should always or never do.
- Keep it short: a few sentences of standing instructions, in the second person ("You are ...", "Prefer ...").
- Describe behaviour, not permissions. A persona can't grant anything; only `coding` does, and the user sees that when blessing.
- Only suggest `"coding": true` when the persona is meant for working inside a code project, and say what it means.

## How to work

1. Check which endpoints and skills exist by proposing `genai --list-endpoints` and `genai --list-skills` for the user to run.
2. Write the profile with `write_file` to `NAME.json` in the working directory. Your file tools can't reach `~/.config/genai/`, and that is by design.
3. Tell the user the next steps, which only they can do:
   - Review the file, then bless it: `genai --bless ./NAME.json`. genai shows the persona text, the endpoint, skills and whether it grants coding mode, and asks them to confirm. A blessed profile can be used by name from where it is.
   - Optionally move it to `~/.config/genai/profiles/NAME.json` first, and bless it there.
   - The endpoint and skills it names must be blessed on their own.
   - Try it: `genai -p NAME "hello"`.
   - To make it the default, set `"default_profile": "NAME"` in `~/.config/genai/config.json`. Only the user does this.
4. If the profile is edited later, genai refuses it until it is blessed again. That is expected.
