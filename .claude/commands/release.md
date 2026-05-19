Ask the user: "What is the new version number? (e.g. 1.4.0)"

Once they provide the version number, validate it:

- It must match the format `MAJOR.MINOR.PATCH` (e.g. `1.4.0`, `2.0.1`) - three numeric segments separated by dots
- It must be higher than the current version in `custom_components/zendo/manifest.json` (compare major, then minor, then patch)
- If the version is invalid, tell the user why and ask again

Then:

1. Update the `version` field in `custom_components/zendo/manifest.json` to the new version
2. Stage the file: `git add custom_components/zendo/manifest.json`
3. Commit with the message `v<VERSION>` (e.g. `v1.4.0`) - do NOT include a Co-Authored-By line
4. Create a git tag with the version number (e.g. `git tag 1.4.0`)
5. Push the commit and tag: `git push && git push --tags`
