"""CI's subprocess verifiers must exist and propagate failure before E2E seeding."""

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


def test_operations_gate_children_exist_and_propagate_failure_with_deadline():
    tree = ast.parse((BACKEND / 'scripts/verify_operations_postgres.py').read_text())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and ast.unparse(node.func) == 'subprocess.run'
    ]
    assert calls, 'The live inventory API verifier must run before E2E seeding'
    modules = set()
    for call in calls:
        command = call.args[0]
        assert isinstance(command, ast.List)
        assert ast.literal_eval(command.elts[1]) == '-m'
        module = ast.literal_eval(command.elts[2])
        assert (BACKEND / (module.replace('.', '/') + '.py')).is_file(), f'Missing verifier: {module}'
        modules.add(module)
        arguments = {keyword.arg: keyword.value for keyword in call.keywords}
        assert ast.literal_eval(arguments['check']) is True
        assert ast.literal_eval(arguments['timeout']) == 60
        assert ast.unparse(arguments['stdin']) == 'subprocess.DEVNULL'
    assert 'scripts.verify_stock_piece_api_postgres' in modules
