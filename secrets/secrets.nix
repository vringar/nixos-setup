let
  vringar = builtins.readFile ../home-manager/files/ssh/github_key.pub;
in {
  "wg-sect.age".publicKeys = [vringar];
}
