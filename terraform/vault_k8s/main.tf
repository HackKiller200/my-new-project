terraform {
	required_providers {
		kubernetes = {
			source = "hashicorp/kubernetes"
			version ="~>2.23"
			}
		helm = {
			source = "hashicorp/helm"
			version = "~>2.11"
			}
	}
}

provider "kubernetes" {
	config_path = "~/.kube/config"
	}

provider "helm" {
	kubernetes {
		config_path = "~/.kube/config"
	}
}

resource "kubernetes_namespace_v1" "vault" {
	metadata {
		name = "vault"
		labels = {
			name = "vault"
			environment = "production"
			}
		}
}

resource "helm_release" "vault" {
	name = "helm"
	repository = "https://helm.releases.hashicorp.com"
	chart = "vault"
	version = "0.27.0"
	namespace = kubernetes_namespace_v1.vault.metadata[0].name
	create_namespace = false
	timeout = 300
	wait = true
	values = [file("${path.module}/values.yaml")]
	depends_on = [kubernetes_namespace_v1.vault]
}




