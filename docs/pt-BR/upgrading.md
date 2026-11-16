# Atualização

[English](../upgrading.md)

## Atualizar o plugin

```sh
hermes plugins update hermes-atlas-evolution                 # o perfil padrão
hermes -p <perfil> plugins update hermes-atlas-evolution     # um perfil com nome

# Todos os perfis com nome
for p in $(ls ~/.hermes/profiles); do hermes -p "$p" plugins update hermes-atlas-evolution; done

hermes gateway restart
sudo systemctl restart hermes-dashboard   # ou do jeito que você roda o dashboard
hermes evolution status
```

O `plugins update` pela linha de comando só troca os arquivos: não recarrega um gateway que já está rodando, então reinicie o gateway uma vez no fim. O dashboard mostra a aba **Evolution** quando relê os plugins; se ela não aparecer, reinicie o dashboard. (O Hermes recarrega um gateway em execução ao instalar, ligar e desligar, e quando a atualização é aplicada pelo Hermes Desktop, pelo dashboard ou por `/plugins update` num chat.) Se algo ainda estiver rodando (um turno, um job de cron, uma tarefa delegada), o reinício lista e pergunta `Interrupt them and restart the gateway? [y/N]`: responda N e rode de novo em alguns minutos, ou y para interromper. O `status` aponta um gateway que ainda roda código antigo.

O que mudou em cada versão: [Mudanças](../../CHANGELOG.pt-BR.md).

## Sair da 0.5.0

Até a 0.5.0 o plugin era instalado como `hermes-evolution`. O id mudou, então o `hermes plugins update` não consegue substituir essa cópia. Em cada perfil que a tem (acrescente `-p <perfil>` aos comandos para um perfil com nome):

1. Só se você mudou as opções do plugin no formulário do Hermes: abra o `config.yaml` do perfil e renomeie `plugins.entries.hermes-evolution` para `plugins.entries.hermes-atlas-evolution` (o próximo comando apaga o bloco antigo).
2. Remova a cópia antiga e instale a nova:

   ```sh
   hermes plugins remove hermes-evolution
   hermes plugins install sesantanajr/hermes-atlas-evolution --enable
   ```

3. Reinicie o gateway uma vez, no seu próprio terminal: `hermes gateway restart`.
4. Confira com `hermes evolution status`. Ele lista os outros perfis que ainda têm a cópia antiga, com o comando exato para cada um, e marca um perfil que tem as duas cópias, onde a antiga ainda é a que roda.

Notas:

- O que foi aprendido fica: o comando `hermes evolution`, os dados em `<HERMES_HOME>/evolution/` e o `settings.json` mantêm os nomes. A primeira passada da 0.6.0 reconta o histórico que a 0.5.0 releu, porque a 0.5.0 contava mensagens que o Hermes guarda em mais de uma cópia.
- Enquanto a cópia antiga estiver ligada, a nova fica inativa (o log do Hermes avisa), então nenhum turno é contado duas vezes. Se o título do `hermes evolution status` num perfil ainda mostra 0.5.0, a cópia antiga continua lá: remova-a nesse perfil e reinicie o gateway.
- Dois padrões mudaram: `lessons` agora é `auto` (`lessons: suggest` mantém o comportamento da 0.5.0; lições que já esperavam continuam esperando), e `tool_hints` agora é `false`.
- Só se você os listou à mão no `config.yaml`: troque `atlas-evolution`, `atlas_evolution_plan` e `atlas_evolution_lessons` por `hermes-atlas-evolution`, `hermes_atlas_evolution_plan` e `hermes_atlas_evolution_lessons` numa lista de toolsets ou em `auxiliary.background_review.extra_tools`.

Na lista de plugins do Hermes o plugin aparece com um cartão genérico: o Hermes mostra uma capa só para entradas do catálogo curado de plugins ([detalhes](hermes-integration.md#capa-e-o-catálogo-de-plugins)).
