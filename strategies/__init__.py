import os
import importlib

STRATEGY_REGISTRY = {}

# Discover all .py files in this folder (except __init__.py)
for file in os.listdir(os.path.dirname(__file__)):
    if file.endswith('.py') and file != '__init__.py':
        module_name = file[:-3]
        try:
            module = importlib.import_module(f'strategies.{module_name}')
            if hasattr(module, 'all_strategies'):
                STRATEGY_REGISTRY[module_name] = module.all_strategies
        except Exception as e:
            print(f"Error loading strategy {module_name}: {e}")

# For backward compatibility, if you still want to import all_strategies from somewhere,
# you can expose the combined dict, but the main app uses STRATEGY_REGISTRY.