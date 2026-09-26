# bwrap on Ubuntu

Ubuntu 23.10 and later set `kernel.apparmor_restrict_unprivileged_userns=1`. With it, an unconfined program cannot create a user namespace with full capabilities (https://ubuntu.com/blog/ubuntu-23-10-restricted-unprivileged-user-namespaces). `bwrap` needs one, so timu's startup probe fails with an error such as:

```
bwrap: setting up uid map: Permission denied
bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted
```

timu then reports `bwrap cannot create a sandbox here`, and roles that run commands do not start.

## Steps

1. Check that `bwrap` is installed, and that the restriction is on:

   ```sh
   bwrap --version                                               # else: sudo apt install bubblewrap
   cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns    # 1: restricted
   ```

2. Write an AppArmor profile that grants `/usr/bin/bwrap` the `userns` rule:

   ```sh
   sudo tee /etc/apparmor.d/bwrap > /dev/null <<'EOF'
   abi <abi/4.0>,
   include <tunables/global>

   profile bwrap /usr/bin/bwrap flags=(unconfined) {
     userns,

     include if exists <local/bwrap>
   }
   EOF
   ```

   The path must match `command -v bwrap`. Edit both occurrences if it differs.

3. Load the profile:

   ```sh
   sudo apparmor_parser -r /etc/apparmor.d/bwrap
   ```

   It stays loaded across reboots, because AppArmor loads every file in `/etc/apparmor.d/` at boot.

4. Check that `bwrap` can create the namespaces timu uses. This is the probe timu runs:

   ```sh
   bwrap --ro-bind / / --unshare-user --unshare-net -- /bin/true && echo ok
   ```

5. Run the enforcement tests, which otherwise skip:

   ```sh
   uv run pytest tests/test_bwrap.py -rs
   ```

   A skip line naming `needs bwrap` means the probe still fails. Its reason follows.

## Undo

```sh
sudo apparmor_parser -R /etc/apparmor.d/bwrap
sudo rm /etc/apparmor.d/bwrap
```

## Trade-off

The profile lets any local program create a user namespace through `bwrap`. The restriction exists to shrink the kernel attack surface that user namespaces expose to unprivileged code. The profile gives up that protection for `bwrap` and anything that runs it.

Alternatives:

- Set `kernel.apparmor_restrict_unprivileged_userns=0`. This lifts the restriction for every program, which is wider.
- Run timu's agents in a VM or container that allows user namespaces (design 15.8, sanduk).
