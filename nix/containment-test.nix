{ pkgs, corecyclerModule }:
let
  probe = pkgs.writeText "corecycler-containment-probe.py" ''
    import json
    import os
    import subprocess
    import sys

    sys.path.insert(0, "${../src}")
    from corecycler.engine import containment

    assert os.geteuid() != 0
    assert containment.available_mechanism(refresh=True) == containment.MECHANISM_USER
    child = (
        "import json, os; "
        "os.sched_setaffinity(0, range(os.cpu_count())); "
        "print(json.dumps(sorted(os.sched_getaffinity(0))))"
    )
    for cpus in [(0,), (1,), (0, 1)]:
        result = subprocess.run(
            containment.contain(cpus).prefix + [sys.executable, "-c", child],
            capture_output=True, text=True, timeout=30, check=True,
        )
        observed = json.loads(result.stdout)
        assert observed == list(cpus), (cpus, observed)
        print(f"requested={cpus}, observed={observed}")
  '';
in
pkgs.testers.runNixOSTest {
  name = "corecycler-user-containment";

  nodes.machine = {
    imports = [ corecyclerModule ];
    services.corecycler = {
      enable = true;
      deviceAccess = false;
      ryzenSmu = false;
    };
    users.users.tester = {
      isNormalUser = true;
      uid = 1000;
      linger = true;
    };
    virtualisation.cores = 2;
  };

  testScript = ''
    machine.start()
    machine.wait_for_unit("user@1000.service")
    print(machine.succeed("su - tester -c 'XDG_RUNTIME_DIR=/run/user/1000 ${pkgs.python3}/bin/python ${probe}'"))
    controllers = machine.succeed("systemctl show user@1000.service -p DelegateControllers --value").split()
    assert {"cpu", "cpuset", "memory", "pids"} <= set(controllers), controllers
  '';
}
