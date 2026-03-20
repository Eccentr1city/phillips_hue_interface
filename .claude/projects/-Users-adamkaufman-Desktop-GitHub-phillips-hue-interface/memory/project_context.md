---
name: project_context
description: Purpose and deployment context for the phillips_hue_interface repo
type: project
---

This repo is a clean, portable Philips Hue control library designed to be cloned and deployed in multiple places.

**First deployment:** Friends' house, integrated into their custom Claude scaffold ("Hob") that controls house properties. Will be set up from Adam's laptop, then continued on the PC where Hob lives.

**Second deployment:** Adam's own home with his own separate Hue bridge.

**Why:** The API should be clean and easily usable so it can plug into different scaffolds/contexts. Portability across bridge instances matters — credentials are per-deployment, not baked in.

**How to apply:** Keep the code bridge-agnostic. Config (bridge IP, API keys, client keys) should be environment-driven. Include a setup script that handles bridge pairing interactively.
