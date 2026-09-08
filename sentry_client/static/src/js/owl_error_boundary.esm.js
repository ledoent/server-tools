// Copyright 2026 Ledoent
// License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
//
// Sentry-aware entry in Odoo's @web/core/error_handlers registry. On the
// backend this is the ONLY capture path: sentry_loader.js switches Sentry's
// own window.onerror / onunhandledrejection handlers off when this module is
// present, so every uncaught error is reported exactly once, with the OWL
// component-tree context, alongside the standard Odoo Oops! dialog flow.
//
// Backend-only: OWL lives under web.assets_backend. Frontend portal/website
// runs on plain templates with no OWL tree (and keeps the global handlers).
/* global window */

import {
    ConnectionAbortedError,
    ConnectionLostError,
    RPCError,
    RequestEntityTooLargeError,
} from "@web/core/network/rpc";
import {registry} from "@web/core/registry";

// Tells the loader (a plain script that runs after the SDK bundle arrives)
// that the registry handler is in place and the global handlers can go.
window.__sentry_client_owl_boundary__ = true;

function buildExtra(target) {
    const extra = {
        event_type: target && target.constructor && target.constructor.name,
    };
    const ct =
        (target && target.componentTree) ||
        (target && target.cause && target.cause.componentTree);
    if (ct) {
        extra.component_tree = ct;
    }
    if (target && target.props !== undefined) {
        extra.props = target.props;
    }
    return extra;
}

// Errors that originate on the server (or on the wire) and that Odoo
// already surfaces in its own dialogs. The server-side `sentry` module
// reports the exception with a full Python traceback; a browser-side copy
// would be a duplicate issue with less information.
function isServerSideError(error) {
    return (
        error instanceof RPCError ||
        error instanceof ConnectionLostError ||
        error instanceof ConnectionAbortedError ||
        error instanceof RequestEntityTooLargeError
    );
}

function noteServerSideError(sdk, error) {
    const data = (error instanceof RPCError && error.data) || {};
    const message = String(data.message || error.message || "").slice(0, 200);
    if (typeof sdk.addBreadcrumb === "function") {
        sdk.addBreadcrumb({
            category: "odoo.rpc",
            level: "error",
            message: `${error.name}: ${message}`,
            data: {exception: error.exceptionName, model: error.model},
        });
    }
    // Replay in buffer mode (error sampling) only uploads when the SDK
    // captures an exception. Backend errors never reach that path, so
    // flush the buffer here: the replay and the server-side event share
    // the trace propagated on the request, and Sentry links them.
    if (!(error instanceof RPCError)) {
        return;
    }
    const replay = typeof sdk.getReplay === "function" && sdk.getReplay();
    if (replay && typeof replay.flush === "function") {
        Promise.resolve(replay.flush()).catch(() => undefined);
    }
}

function sentryHandler(env, error, originalError) {
    const sdk = window.Sentry;
    if (!sdk || typeof sdk.captureException !== "function") {
        return false;
    }
    const conf = window.__sentry_client__ || {};
    if (isServerSideError(originalError) && !conf.captureRpcErrors) {
        noteServerSideError(sdk, originalError);
        return false;
    }
    // Capture the WRAPPING error so Sentry's built-in LinkedErrors integration
    // expands `.cause` into nested exception entries inside ONE event.
    const target = error || originalError;
    sdk.captureException(target, {
        tags: {owl: true},
        extra: buildExtra(target),
    });
    // Return false so Odoo's other handlers also run (the user still sees
    // the standard Oops! dialog, the network request retry chain, etc.).
    return false;
}

// Sequence 1: run before any handler that might return true and stop the
// chain (Odoo's own start at 97; website's visitor swallower sits at 0).
registry
    .category("error_handlers")
    .add("sentry_client.owl", sentryHandler, {sequence: 1});
