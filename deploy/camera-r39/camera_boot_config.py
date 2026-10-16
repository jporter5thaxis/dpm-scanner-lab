"""Add an IMX219 boot choice while preserving the original recovery choice."""
import re


def camera_boot_configuration(contents, overlay, dtb=None, baked=False):
    if baked and not dtb:
        raise ValueError('A baked camera overlay requires an explicit DTB')
    default = re.search(r'^DEFAULT\s+(\S+)\s*$', contents, re.M | re.I)
    if not default:
        raise ValueError('Boot configuration has no explicit default entry')
    labels = list(re.finditer(r'^LABEL\s+(\S+)\s*$', contents, re.M | re.I))
    blocks = {match.group(1): contents[match.start():labels[index+1].start() if index+1 < len(labels) else len(contents)]
              for index, match in enumerate(labels)}
    if 'dpm-imx219' in blocks:
        existing = blocks['dpm-imx219']
        matches = (bool(re.search(r'^\s*FDT\s+'+re.escape(dtb)+r'\s*$', existing, re.M))
                   and overlay not in existing) if baked else overlay in existing
        if default.group(1) == 'dpm-imx219' and matches:
            return contents
        raise ValueError('An existing camera boot choice differs; review is required')
    if default.group(1) not in blocks:
        raise ValueError('Default boot entry was not found')
    lines = [line for line in blocks[default.group(1)].splitlines()
             if line.strip() and not line.lstrip().startswith('#')]
    if not any(re.match(r'\s*LINUX\s+', line, re.I) for line in lines):
        raise ValueError('Default boot entry has no kernel path')
    new = ['LABEL dpm-imx219', '      MENU LABEL IMX219 camera (original boot entry retained)']
    overlay_added = False
    for line in lines[1:]:
        if re.match(r'\s*MENU\s+LABEL\s+', line, re.I):
            continue
        if dtb and re.match(r'\s*FDT\s+',line,re.I):
            continue
        match = re.match(r'\s*OVERLAYS\s+(.+)', line, re.I)
        if match:
            if 'imx219' in match.group(1):
                raise ValueError('A different IMX219 overlay is already configured')
            new.append('      OVERLAYS '+match.group(1).strip()+('' if baked else ','+overlay))
            overlay_added = True
        elif baked and re.match(r'\s*APPEND\s+',line,re.I) and 'dpm_camera=1' not in line.split():
            new.append(line+' dpm_camera=1')
        else:
            new.append(line)
    if not overlay_added and not baked:
        new.append('      OVERLAYS '+overlay)
    if dtb:
        new.append('      FDT '+dtb)
    retained = contents[:default.start()]+'DEFAULT dpm-imx219'+contents[default.end():]
    return retained.rstrip()+'\n\n'+'\n'.join(new)+'\n'
