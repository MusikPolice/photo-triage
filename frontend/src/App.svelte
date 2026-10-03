<script lang="ts">
  import Home from "./Home.svelte";
</script>

<header>
  <span class="name">photo-triage</span>
</header>

<main>
  <Home />
</main>

<style>
  header {
    display: flex;
    align-items: center;
    gap: 1rem;
    padding: 0.75rem 1rem;
    background: var(--header-bg);
    border-bottom: 1px solid var(--border);
  }

  .name {
    font-weight: 600;
  }

  main {
    padding: 1rem;
  }
</style>
