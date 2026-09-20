ControleFin — backup sem senha da nuvem

Base esperada:
    controlefin_google_auth_primary_embedded_oauth

Mudanças:
- novos snapshots são formato v2;
- nenhuma senha da nuvem é necessária para novos backups;
- chave AES-256-GCM aleatória fica em Meu Drive / ControleFin / controlefin-key.json;
- novo PC recupera chave automaticamente após login Google;
- snapshots v1 continuam suportados para migração;
- se a senha antiga ainda está salva no computador original, a migração ocorre
  automaticamente no próximo smart sync;
- caso contrário, a senha antiga é pedida somente uma vez para converter v1 -> v2.

Depois:
    python .\run_tests.py
    .\BUILD_WINDOWS.ps1

Distribua:
    dist\ControleFin-Windows.zip
