<img src="http://mypy-lang.org/static/mypy_light.svg" alt="mypy logo" width="300px"/>

# pytest plugin for testing mypy types, stubs, and plugins

[![Tests Status](https://github.com/typeddjango/pytest-mypy-plugins/actions/workflows/test.yml/badge.svg)](https://github.com/typeddjango/pytest-mypy-plugins/actions/workflows/test.yml)
[![Checked with mypy](http://www.mypy-lang.org/static/mypy_badge.svg)](http://mypy-lang.org/)
[![Gitter](https://badges.gitter.im/mypy-django/Lobby.svg)](https://gitter.im/mypy-django/Lobby)
[![PyPI](https://img.shields.io/pypi/v/pytest-mypy-plugins?color=blue)](https://pypi.org/project/pytest-mypy-plugins/)
[![Conda Version](https://img.shields.io/conda/vn/conda-forge/pytest-mypy-plugins.svg?color=blue)](https://anaconda.org/conda-forge/pytest-mypy-plugins)

## Installation

This package is available on [PyPI](https://pypi.org/project/pytest-mypy-plugins/)

```bash
pip install pytest-mypy-plugins
```

and [conda-forge](https://anaconda.org/conda-forge/pytest-mypy-plugins)

```bash
conda install -c conda-forge pytest-mypy-plugins
```

## Usage

### Running

Plugin, after installation, is automatically picked up by `pytest` therefore it is sufficient to
just execute:

```bash
pytest
```

### Asserting types

There are two ways to assert types.
The custom one and regular [`typing.assert_type`](https://docs.python.org/3/library/typing.html#typing.assert_type).

Our custom type assertion uses `reveal_type` helper and custom output matchers:

```yml
- case: using_reveal_type
  main: |
    instance = 1
    reveal_type(instance)  # N: Revealed type is 'builtins.int'
```

This method also allows to use `# E:` for matching exact error messages and codes.

But, you can also use regular `assert_type`, examples can be [found here](https://github.com/typeddjango/pytest-mypy-plugins/blob/master/pytest_mypy_plugins/tests/test-assert-type.yml).

### Paths

The `PYTHONPATH` and `MYPYPATH` environment variables, if set, are passed to `mypy` on invocation.
This may be helpful if you are testing a local plugin and need to provide an import path to it.

Be aware that when `mypy` is run in a subprocess (the default) the test cases are run in temporary working directories
where relative paths such as `PYTHONPATH=./my_plugin` do not reference the directory which you are running `pytest` from.
If you encounter this, consider invoking `pytest` with `--mypy-same-process` or make your paths absolute,
e.g. `PYTHONPATH=$(pwd)/my_plugin pytest`.

You can also specify `PYTHONPATH`, `MYPYPATH`, or any other environment variable in `env:` section of `yml` spec:

```yml
- case: mypy_path_from_env
  main: |
    from pair import Pair

    instance: Pair
    reveal_type(instance)  # N: Revealed type is 'pair.Pair'
  env:
    - MYPYPATH=../fixtures
```


### What is a test case?

In general each test case is just an element in an array written in a properly formatted `YAML` file.
On top of that, each case must comply to following types:

| Property        | Type                                                   | Description                                                                                                         |
| --------------- | ------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------- |
| `case`          | `str`                                                  | Name of the test case, complies to `[a-zA-Z0-9]` pattern                                                            |
| `main`          | `str`                                                  | Portion of the code as if written in `.py` file                                                                     |
| `start`         | `str`                                                  | The file or folder that mypy starts checking from. Defaults to main.py                                              |
| `files`         | `Optional[List[File]]=[]`\*                            | List of extra files to simulate imports if needed                                                                   |
| `disable_cache` | `Optional[bool]=False`                                 | Set to `true` disables `mypy` caching                                                                               |
| `mypy_config`   | `Optional[str]`                                        | Inline `mypy` configuration, passed directly to `mypy` as `--config-file` option, possibly joined with `--mypy-pyproject-toml-file` or `--mypy-ini-file` contents if they are passed. By default is treated as `ini`, treated as `toml` only if `--mypy-pyproject-toml-file` is passed |
| `env`           | `Optional[Dict[str, str]]={}`                          | Environmental variables to be provided inside of test run                                                           |
| `parametrized`  | `Optional[List[Parameter]]=[]`\*                       | List of parameters, similar to [`@pytest.mark.parametrize`](https://docs.pytest.org/en/stable/parametrize.html)     |
| `skip`          | `str`                                                  | Expression evaluated with following globals set: `sys`, `os`, `pytest` and `platform`                               |
| `expect_fail`   | `bool`                                                 | Mark test case as an expected failure, like [`@pytest.mark.xfail`](https://docs.pytest.org/en/stable/skipping.html) |
| `regex`         | `str`                                                  | Allow regular expressions in comments to be matched against actual output. Defaults to "no", i.e. matches full text.|
| `followups`     | `Optional[List[Followup]]=[]`*                         | Allow specifying changes to the files for followup runs of mypy                                                     |

(*) Appendix to **pseudo** types used above:

```python
class File:
    path: str
    content: str = ""

class FollowupFile:
    path: str
    # Content must be specified
    # an empty string will keep the file existing, but empty
    # null will delete the file
    content: Optional[str]

Parameter = Mapping[str, Any]

class Followup:
    # if main is None it is unchanged
    main: Optional[str] = None
    description: str = ""
    files: List[FollowupFile] = []
    skip: bool | str = False
    out: Optional[str] = None
    expect_fail: Optional[bool] = None
```

Implementation notes:

- `main` must be non-empty string that evaluates to valid **Python** code,
- `content` of each of extra files must evaluate to valid **Python** code,
- `parametrized` entries must all be the objects of the same _type_. It simply means that each
  entry must have **exact** same set of keys,
- `skip` - an expression set in `skip` is passed directly into
  [`eval`](https://docs.python.org/3/library/functions.html#eval). It is advised to take a peek and
  learn about how `eval` works.
- An empty followup means the code is unchanged and mypy will be run again with the same `out` and `expect_fail`

Repository also offers a [JSONSchema](pytest_mypy_plugins/schema.json), with which
it validates the input. It can also offer your editor auto-completions, descriptions, and validation.

All you have to do, add the following line at the top of your YAML file:
```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/typeddjango/pytest-mypy-plugins/master/pytest_mypy_plugins/schema.json
```

### Example

#### 1. Inline type expectations

```yaml
# typesafety/test_request.yml
- case: request_object_has_user_of_type_auth_user_model
  main: |
    from django.http.request import HttpRequest
    reveal_type(HttpRequest().user)  # N: Revealed type is 'myapp.models.MyUser'
    # check that other fields work ok
    reveal_type(HttpRequest().method)  # N: Revealed type is 'Union[builtins.str, None]'
  files:
    - path: myapp/__init__.py
    - path: myapp/models.py
      content: |
        from django.db import models
        class MyUser(models.Model):
            pass
```

#### 2. `@parametrized`

```yaml
- case: with_params
  parametrized:
    - val: 1
      rt: builtins.int
    - val: 1.0
      rt: builtins.float
  main: |
    reveal_type({{ val }})  # N: Revealed type is '{{ rt }}'
```

Properties that you can parametrize:
- `main`
- `mypy_config`
- `out`

#### 3. Longer type expectations

```yaml
- case: with_out
  main: |
    reveal_type('abc')
  out: |
    main:1: note: Revealed type is 'builtins.str'
```

#### 4. Regular expressions in expectations

```yaml
- case: expected_message_regex_with_out
  regex: yes
  main: |
    a = 'abc'
    reveal_type(a)
  out: |
    main:2: note: .*str.*
```

#### 5. Regular expressions specific lines of output.

```yaml
- case: expected_single_message_regex
  main: |
    a = 'hello'
    reveal_type(a)  # NR: .*str.*
```

## Options

```
mypy-tests:
  --mypy-testing-base=MYPY_TESTING_BASE
                        Base directory for tests to use
  --mypy-pyproject-toml-file=MYPY_PYPROJECT_TOML_FILE
                        Which `pyproject.toml` file to use as a default config for tests. Incompatible with `--mypy-ini-file`
  --mypy-ini-file=MYPY_INI_FILE
                        Which `.ini` file to use as a default config for tests. Incompatible with `--mypy-pyproject-toml-file`
  --mypy-same-process   Run in the same process. Useful for debugging, will create problems with import cache
  --mypy-extension-hook=MYPY_EXTENSION_HOOK
                        Fully qualified path to the extension hook function, in case you need custom yaml keys. Has to be top-level.
  --mypy-scenario-hook=MYPY_SCENARIO_HOOK
                        Fully qualified path to the scenario hook maker
  --mypy-only-local-stub
                        mypy will ignore errors from site-packages
  --mypy-closed-schema  Use closed schema to validate YAML test cases, which won't allow any extra keys (does not work well with `--mypy-extension-
                        hook`)
  --mypy-cache-strategy={SHARED_INCREMENTAL,NO_INCREMENTAL,NON_SHARED_INCREMENTAL}
                        The strategy used by the plugin
                        SHARED_INCREMENTAL (default)
                        - mypy only run once each time. disable_cache doesn't change incremental setting
                        - not setting disable_cache uses a shared cache where files are deleted from it
                        - after each time mypy is run
                        NO_INCREMENTAL
                        - mypy is run one for each run with --no-incremental. The disable-cache option
                        - does nothing in this strategy
                        NON_SHARED_INCREMENTAL
                        - mypy is run twice for each run with --incremental.
                        - First with an empty cache relative to the temporary directory
                        - and again after that cache is made.
                        - The disable-cache option prevents the second run in this strategy

```

## Hooks

There are two types of hooks that can be provided: extension hook and
scenario hook.

The extension hook can be used to modify the options for a yaml test case before
anything is done, whereas the scenario hook may be used to perform actions before
each run of mypy (especially useful for test cases with followup actions).

To use an extension hook, either the `--mypy-extension-hook` option is used to
provide the import path to a function, which would be specified as follows:

```python
from typing import TYPE_CHECKING

from pytest_mypy_plugins import ExtensionHook, ItemForHook


def hook(item: ItemForHook) -> None:
    # perform hook here
    pass


if TYPE_CHECKING:
    _h: ExtensionHook = hook
```

To create a scenario hook would be providing `--mypy-scenario-hooks` to an import
path for a callable object that takes in no arguments and returns a
`pytest_mypy_plugins.ScenarioHooks` object. It would look like:


```
from collections.abc import Mapping, MutableSequence
from typing import TYPE_CHECKING

from pytest_mypy_plugins import (
    ScenarioHooks,
    ScenarioHookMaker,
    MypyPluginsScenario,
    OutputMatcher,
    ScenarioHooksRunAndCheckOptions,
)


class Hooks(ScenarioHooks):
    def before_run_and_check_mypy(
        self,
        *,
        scenario: MypyPluginsScenario,
        options: ScenarioHooksRunAndCheckOptions,
        config_file: pathlib.Path,
        expected_output: MutableSequence[OutputMatcher],
        additional_properties: Mapping[str, object],
    ) -> ScenarioHooksRunAndCheckOptions:
        """
        Used to do any adjustments to the scenario before running mypy

        Must return the a ScenarioHooksRunAndCheckOptions object.

        If it's desirable to return the one provided but with different options, ``dataclasses.replace``
        is a good idea: ``return dataclasses.replace(options, expect_fail=True)``
        """
        return options


if TYPE_CHECKING:
  # Note that a class based off ScenarioHooks is already a valid hook maker
  # But this will also work if you use some other function to return an instance
  # of the hooks
  _sh: ScenarioHookMaker = Hooks
```

## Further reading

- [Testing mypy stubs, plugins, and types](https://sobolevn.me/2019/08/testing-mypy-types)

## License

[MIT](https://github.com/typeddjango/pytest-mypy-plugins/blob/master/LICENSE)
