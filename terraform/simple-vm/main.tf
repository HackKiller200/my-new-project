terraform {
	required_providers {
		yandex = {
			source = "yandex-cloud/yandex"
			version = "0.191.0"
			}
	}
}

provider "yandex" {
	token = var.token
	cloud_id = var.cloud_id
	folder_id = var.folder_id
	zone = "ru-central1-a"
}

resource "yandex_compute_instance" "vm-1" {
	name = "linux-vm"
	platform_id = "standard-v1"
	resources {
		cores = var.cpu
		memory = var.ram
}
	boot_disk {
		initialize_params {
			image_id = "fd8tb05vfocj9h49m08d"
			}
		}
	network_interface {
		subnet_id = yandex_vpc_subnet.subnet-1.id
		nat = true
		}
	metadata = {
		ssh-keys = "${var.name_user}:${var.ssh_key}"
		}
}


resource "yandex_vpc_network" "network-1" {
	name = "network-1"
}

resource "yandex_vpc_subnet" "subnet-1" {
	name = "subnet-1"
	network_id = yandex_vpc_network.network-1.id
	v4_cidr_blocks = ["10.10.10.0/24"]
	zone = "ru-central1-a"
}
