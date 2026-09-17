def classFactory(iface):
    """Load BuildingRoadOverlapValidatorPlugin class from plugin.py."""
    from .plugin import BuildingRoadOverlapValidatorPlugin
    return BuildingRoadOverlapValidatorPlugin(iface)