"""Canonical work identity shared by catalog selection and structure replay."""

def canonical_item(doc, identity):
    aliases = {old: change["to_ids"][0] for change in doc.get("identity_changes", [])
               if change.get("kind") == "item_merge" and len(change.get("to_ids", [])) == 1
               for old in change.get("from_ids", [])}
    seen = set()
    while identity in aliases and identity not in seen:
        seen.add(identity)
        identity = aliases[identity]
    return identity
