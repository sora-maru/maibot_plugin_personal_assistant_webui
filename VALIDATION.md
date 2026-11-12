# Validation

## Local checks

- `plugin.py` passes Python bytecode compilation.
- `tests/test_webui_api.py` passes all three standard-library unit tests for
  filtering/field allowlisting, pending task cancellation and media cleanup,
  and refusal to cancel an in-progress reminder.
- `_manifest.json` and `webui.json` parse as JSON; the page entry and custom
  page capability agree.
- The embedded page JavaScript passes Node syntax parsing.

## Not verified in this workspace

- `maibot_sdk` is not installed in the local Python environment, so SDK loading,
  page registration, and live MaiBot behavior were not exercised.
- No MaiBot instance or QQ connection was used to create, deliver, or cancel a
  real reminder.
- The new public GitHub repository and its MaiBot Plugin Registry submission
  have not been created or reviewed.
