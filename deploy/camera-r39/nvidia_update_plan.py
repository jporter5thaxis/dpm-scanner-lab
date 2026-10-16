"""Reject changes outside the coordinated, installed NVIDIA R39.2.1 package set."""
import re

TARGET = '39.2.1-20260806224157'


def checked_changes(plan, installed):
    changes = {}
    for line in plan.splitlines():
        if line.startswith('Remv '):
            raise ValueError('The update would remove a package')
        if not line.startswith('Inst '):
            continue
        match = re.match(r'Inst (\S+)(?: \[([^]]+)\])? \((\S+)', line)
        if not match:
            raise ValueError('Unrecognized package change: '+line)
        package, old, new = match.groups()
        package = package.removesuffix(':arm64')
        if not package.startswith('nvidia-l4t-') or package not in installed or not old:
            raise ValueError('Unexpected package change: '+package)
        if old != installed[package]:
            raise ValueError('Installed package version changed: '+package)
        if new not in (TARGET, '6.8.12-tegra-'+TARGET):
            raise ValueError('Unexpected target version: '+package+' '+new)
        if old not in ('39.2.0-20260601141651', '6.8.12-tegra-39.2.0-20260601141651'):
            raise ValueError('Unexpected starting version: '+package+' '+old)
        changes[package] = new
    if not changes:
        raise ValueError('No R39.2.0 to R39.2.1 update was found')
    return changes
