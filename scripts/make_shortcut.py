r"""Build the iOS shortcut "Paper Book Sync": photo -> on-device OCR -> <bridge>/sync?text=...

    python3 scripts/make_shortcut.py [default-bridge-url]       # -> shortcuts/Paper Book Sync.unsigned.shortcut
    shortcuts sign --mode anyone -i "shortcuts/Paper Book Sync.unsigned.shortcut" -o "shortcuts/Paper Book Sync.shortcut"
    open "shortcuts/Paper Book Sync.shortcut"                 # macOS asks for the bridge URL, then [Add Shortcut]

On import, Shortcuts asks for the bridge URL (an import question on the first Text action).
"""
import pathlib, plistlib, sys, uuid

default = (sys.argv[1] if len(sys.argv) > 1 else "http://192.168.1.10:8086").rstrip("/")
U = lambda: str(uuid.uuid4()).upper()
u_base, u_photo, u_text, u_enc, u_open = U(), U(), U(), U(), U()
ref = lambda u, name: {"OutputUUID": u, "Type": "ActionOutput", "OutputName": name}
attach = lambda u, name: {"Value": ref(u, name), "WFSerializationType": "WFTextTokenAttachment"}
utf16 = lambda s: len(s.encode("utf-16-le")) // 2


def text(*parts):
    """Token string from literal strings and (uuid, name) variables; text parameters need this, not a bare attachment."""
    s, refs = "", {}
    for p in parts:
        if isinstance(p, tuple):
            refs[f"{{{utf16(s)}, 1}}"] = ref(*p)
            s += "￼"
        else:
            s += p
    return {"Value": {"string": s, "attachmentsByRange": refs}, "WFSerializationType": "WFTextTokenString"}


wf = {
    "WFWorkflowClientVersion": "3607.0.2", "WFWorkflowMinimumClientVersion": 900, "WFWorkflowMinimumClientVersionString": "900",
    "WFWorkflowIcon": {"WFWorkflowIconStartColor": 463140863, "WFWorkflowIconGlyphNumber": 59511},
    "WFWorkflowImportQuestions": [{
        "ActionIndex": 0, "Category": "Parameter", "ParameterKey": "WFTextActionText", "DefaultValue": default,
        "Text": "ereader-bridge address (no trailing slash), e.g. http://192.168.1.10:8086"}],
    "WFWorkflowTypes": [], "WFWorkflowInputContentItemClasses": [],
    "WFWorkflowOutputContentItemClasses": [], "WFWorkflowHasOutputFallback": False,
    "WFWorkflowHasShortcutInputVariables": False, "WFQuickActionSurfaces": [],
    "WFWorkflowActions": [
        {"WFWorkflowActionIdentifier": "is.workflow.actions.gettext",
         "WFWorkflowActionParameters": {"UUID": u_base, "WFTextActionText": default}},
        {"WFWorkflowActionIdentifier": "is.workflow.actions.takephoto",
         "WFWorkflowActionParameters": {"UUID": u_photo, "WFCameraCaptureShowPreview": True, "WFPhotoCount": 1}},
        {"WFWorkflowActionIdentifier": "is.workflow.actions.extracttextfromimage",
         "WFWorkflowActionParameters": {"UUID": u_text, "WFImage": attach(u_photo, "Photo")}},
        {"WFWorkflowActionIdentifier": "is.workflow.actions.urlencode",
         "WFWorkflowActionParameters": {"UUID": u_enc, "WFEncodeMode": "Encode", "WFInput": text((u_text, "Extracted Text"))}},
        {"WFWorkflowActionIdentifier": "is.workflow.actions.openurl",
         "WFWorkflowActionParameters": {"UUID": u_open, "Show-WFInput": True,
                                        "WFInput": text((u_base, "Text"), "/sync?text=", (u_enc, "URL Encoded Text"))}},
    ],
}
out = pathlib.Path(__file__).resolve().parent.parent / "shortcuts" / "Paper Book Sync.unsigned.shortcut"
out.parent.mkdir(exist_ok=True)
out.write_bytes(plistlib.dumps(wf, fmt=plistlib.FMT_BINARY))
print(out)
