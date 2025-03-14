import os
import re
import yaml

class EnvVarLoader(yaml.SafeLoader):
    pass

def env_var_constructor(loader, node):
    value = loader.construct_scalar(node)
    
    env_var_match = re.match(r'\$\{(\w+):?(.*)?\}', value)
    if env_var_match:
        env_var_name = env_var_match.group(1)
        default_value = env_var_match.group(2) or ""
        return os.getenv(env_var_name, default_value)
    else:
        return value

# Đăng ký constructor cho tag `!ENV`
EnvVarLoader.add_constructor('!ENV', env_var_constructor)

def load_rule_paths(filename):
    with open(filename, 'r') as file:
        data = yaml.load(file, Loader=EnvVarLoader)
    return data

# Đường dẫn đến file quy tắc
rules = load_rule_paths('rules/check_down_service.yaml')
print(rules)
