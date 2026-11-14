"""Gap-preserving index membership compression; ranges are not connected geometry."""
import ast


def index_runs(indices):
    vals=[]
    for i in indices:
        if isinstance(i,bool) or not isinstance(i,int) or i<0:
            raise ValueError
        vals.append(i)
    runs=[]
    for i in sorted(set(vals)):
        if runs and i==runs[-1][1]+1:
            runs[-1][1]=i
        else:
            runs.append([i,i])
    return runs


def ast_review(source):
    tree = ast.parse(source)
    allowed_nodes = (ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Assign, ast.Name,
                     ast.Store, ast.Load, ast.List, ast.For, ast.If, ast.Call, ast.Attribute,
                     ast.Compare, ast.BoolOp, ast.Or, ast.And, ast.UnaryOp, ast.Not, ast.Constant,
                     ast.Return, ast.Raise, ast.Subscript, ast.BinOp, ast.Add, ast.Eq, ast.Lt,
                     ast.USub, ast.Expr)
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        raise ValueError("one reviewed function only")
    function = tree.body[0]
    if (function.name != "index_runs" or function.decorator_list or function.returns or
            [a.arg for a in function.args.args] != ["indices"] or function.args.defaults or
            function.args.posonlyargs or function.args.kwonlyargs or function.args.vararg or function.args.kwarg):
        raise ValueError("unexpected callable signature")
    names = {"indices", "vals", "i", "runs", "isinstance", "bool", "int", "ValueError", "sorted", "set"}
    for node in ast.walk(tree):
        if not isinstance(node, allowed_nodes):
            raise ValueError("unreviewed AST node: " + type(node).__name__)
        if isinstance(node, ast.Name) and node.id not in names:
            raise ValueError("unreviewed name: " + node.id)
        if isinstance(node, ast.Call):
            call = node.func
            if isinstance(call, ast.Name) and call.id not in {"isinstance", "sorted", "set"}:
                raise ValueError("unreviewed call")
            if isinstance(call, ast.Attribute) and not (isinstance(call.value, ast.Name) and call.value.id in {"vals", "runs"} and call.attr == "append"):
                raise ValueError("unreviewed attribute call")
            if not isinstance(call, (ast.Name, ast.Attribute)):
                raise ValueError("unreviewed dynamic call")
    return {"one_function": True, "imports": 0, "io_calls": 0, "external_calls": 0,
            "global_declarations": 0, "node_count": sum(1 for _ in ast.walk(tree)),
            "review_type": "MANUAL_SOURCE_INSPECTION_AND_AST_ALLOWLIST_NOT_A_GENERAL_SANDBOX"}


def expand(runs):
    return [i for a, b in runs for i in range(a, b + 1)]
