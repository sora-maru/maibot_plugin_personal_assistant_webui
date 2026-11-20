# Third-party notices

## Personal Assistant

The reminder implementation in `plugin.py` is derived from LDrumDusk's
`maibot_personal_assistant`, available at
https://github.com/LDrumDusk/maibot_personal_assistant .

The upstream repository is licensed under the MIT License. Its copyright and
license notice are retained in `LICENSE`. This fork adds a MaiBot WebUI task
manager and adapts the plugin manifest and documentation for this distribution.

Version 1.3.0 targets MaiBot v1.4.7 and Plugin SDK v2.23.0. Compatibility tools
read a separately supplied upstream checkout; this package does not bundle the
Host or SDK source. SDK dependencies are installed by the runtime/developer.
