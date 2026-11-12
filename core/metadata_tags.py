"""
metadata_tags.py
-----------------
Embeds the IPTC Photo Metadata Standard's "Data Mining" property (added
in v2023.1, Oct 2023, developed with the PLUS Coalition) as an XMP
packet -- the actual, current, real standard for expressing "don't use
this image to train AI" in a machine-readable way that travels with the
file. Namespace and controlled-vocabulary URIs below are the real ones
IPTC/PLUS publish and ExifTool (12.67+) recognizes -- verified against
IPTC's own announcement and ExifTool documentation, not guessed.

BE HONEST ABOUT WHAT THIS DOES AND DOESN'T DO
    This is a *declared preference*, not a technical barrier -- nothing
    stops a scraper from ignoring it, same as robots.txt. Worse: IPTC's
    own research says platforms routinely strip embedded metadata on
    upload "in the misguided belief that it will improve site
    performance" -- so this tag frequently won't even survive being
    posted to Instagram/X/etc. It's real, it's the actual current
    standard, compliant tools and any AI developer complying with EU AI
    Act Article 4(3) opt-out requirements will respect it -- but it is
    not a substitute for the cloak/watermark, which is why Ward applies
    both. Think of this as: free to add, meaningful where it survives,
    not something to rely on alone.
"""
from __future__ import annotations

_PLUS_NS = "http://ns.useplus.org/ldf/xmp/1.0/"
_DATA_MINING_PROHIBIT_AI_TRAINING = "https://ns.useplus.org/ldf/vocab/DMI-PROHIBITED-AIMLTRAINING"


def build_xmp_packet(creator_tool: str = "Ward") -> bytes:
    """A minimal, valid XMP packet declaring plus:DataMining =
    "prohibited for AI/ML training" (covers both generative and
    non-generative AI/ML training; does not also block search-engine
    indexing -- see DMI-PROHIBITED if you want the broader value
    instead, or DMI-PROHIBITED-GENAIMLTRAINING for the narrower one).
    UTF-8 encoded, ready to pass as Image.save(..., xmp=...) for JPEG or
    embed as a PNG iTXt "XML:com.adobe.xmp" chunk (see pipeline.py)."""
    packet = f"""<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:plus="{_PLUS_NS}"
    xmlns:xmp="http://ns.adobe.com/xap/1.0/">
   <plus:DataMining>{_DATA_MINING_PROHIBIT_AI_TRAINING}</plus:DataMining>
   <xmp:CreatorTool>{creator_tool}</xmp:CreatorTool>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>"""
    return packet.encode("utf-8")
