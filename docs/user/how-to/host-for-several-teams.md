# Host Grimoire for several teams

This guide shows how to serve several teams from one Grimoire, so that each team sees its own repositories and nobody else's noise. It uses [spaces](../reference/configuration.md#spaces) and assumes Grimoire is already [deployed](deploy.md).

!!! warning "Spaces filter the view; they are not a security boundary"

    Every team shares one GitHub token, one git identity, one workspace and one database. Anyone who can open Grimoire can select any space, and anyone who can edit the configuration or a check or action script is effectively an administrator of the whole instance. Do not use spaces to keep teams apart; use them to keep the screens short.

## Give each team a space file

1. Create a `spaces.d/` directory next to your `config.yaml`. The paths in `space_files` are relative to the directory of the main config file, so in the Docker setup of [Deploy Grimoire](deploy.md#docker-compose) it lives in the mounted `config/` directory.

2. Add one file per team, with the keys of one [`spaces` entry](../reference/configuration.md#spaces):

    ```yaml
    # config/spaces.d/payments.yaml
    id: payments
    name: "Payments"
    color: "#e8590c"
    repositories:
      - team: "org-name/payments"
      - repo: "org-name/shared-lib"
        workflows:
          include: ["CI"]
    ```

3. Point the main config at the directory:

    ```yaml
    # config/config.yaml
    space_files: ["spaces.d/*.yaml"]
    ```

4. Restart Grimoire. The navigation bar now shows a space selector, and the dashboard, backlog, checks, actions and PRs pages follow it.

The `id` must be unique across all files and any inline `spaces`; a duplicate stops the start-up with a message naming both places. A repository can appear in several spaces; it is fetched once.

Repositories that stay in the top-level `repositories` and in no space appear under **Unassigned**. To show nothing there, move them into a space.

## Let each team own its file

Because each space is a file, you can use your Git host's ownership rules to let a team change only its own space. With a `CODEOWNERS` file on GitHub:

```
/config/spaces.d/payments.yaml  @org-name/payments
/config/spaces.d/platform.yaml  @org-name/platform
/config/config.yaml             @org-name/grimoire-admins
```

Keep `config.yaml`, `data/` (checks and actions) and `mise.toml` owned by the people who run the instance: a space file can only add repositories to the view, but a check or action script runs with the instance's token and privileges.

## Scope checks and actions to a space

A check or action without `spaces` is listed under every space. To list one only under some spaces, add the ids:

```yaml
# data/checks/payments-lock-fresh.yaml
name: Payments lock is fresh
spaces: [payments]
# ...
```

This only changes where the check is **listed**. Which repositories it runs on is still decided by `targets`. An unknown id stops the start-up and names the check. See the [checks](../reference/checks.md) and [actions](../reference/actions.md) references.

## Put Grimoire behind your login

Grimoire has no authentication. For a shared instance, put it behind a VPN or an identity-aware proxy, so that only your colleagues can reach it.

- Let the proxy skip authentication for `/health` and `/metrics`, so that container probes and Prometheus keep working. If the instance is served [under a path prefix](deploy.md#serving-under-a-path-prefix-ingress), the paths carry the prefix (for example `/grimoire/metrics`).
- Treat the instance as one trust domain: everyone who can sign in can see every space and trigger every check and action.

## Check the result

1. Open Grimoire and use the selector in the navigation bar to pick one space. Every page now lists only that space's repositories, and the dashboard counts only those.
2. Select two spaces. A repository that belongs to both appears once.
3. Reload the page, or close and reopen the browser: the selection is kept in the browser for a year.
4. Call `GET /api/spaces` to see the spaces and their repository counts, and `GET /api/repos?spaces=payments` to filter the API. The API ignores the selection saved in the browser; see [Filtering by space](../reference/api.md#filtering-by-space).

To report per space in Prometheus, join on the `repo` label with `grimoire_repository_space_info`; `METRICS.md` has the query.
