#!/bin/bash
set -e

sudo apt update && sudo apt upgrade -y
sudo swapoff -a
#sudo sed -i '/swap/ s/^/#/' /etc/fstab && sudo swapoff -a && sudo systemctl mask swap.target
echo "Swap off"
sleep 5

sudo modprobe overlay
sudo modprobe br_netfilter
echo "Installing modules"
sleep 5

sudo tee /etc/sysctl.d/k8s.conf > /dev/null <<EOF
net.bridge.bridge-nf-call-iptables = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward = 1
EOF
sudo sysctl --system
sleep 5

sudo apt update
sudo apt install -y containerd
sudo mkdir -p /etc/containerd
containerd config default | sudo tee /etc/containerd/config.toml
sudo sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
sudo sed -i 's/pause:3\.8/pause:3.10/' /etc/containerd/config.toml
sudo systemctl restart containerd
sudo systemctl enable containerd
echo "Containerd ready"
sleep 5

cd /tmp
sudo curl -L --retry 5 --retry-delay 3 -o /usr/local/bin/kubeadm "https://dl.k8s.io/release/v1.31.0/bin/linux/amd64/kubeadm"
sudo curl -L --retry 5 --retry-delay 3 -o /usr/local/bin/kubelet "https://dl.k8s.io/release/v1.31.0/bin/linux/amd64/kubelet"
sudo curl -L --retry 5 --retry-delay 3 -o /usr/local/bin/kubectl "https://dl.k8s.io/release/v1.31.0/bin/linux/amd64/kubectl"
sudo chmod 755 /usr/local/bin/kubeadm
sudo chmod 755 /usr/local/bin/kubelet
sudo chmod 755 /usr/local/bin/kubectl
echo "Kubelet,ctl,adm install"
sleep 5

hash -r
sudo ln -sf /usr/local/bin/kubeadm /usr/bin/kubeadm
sudo ln -sf /usr/local/bin/kubelet /usr/bin/kubelet
sudo ln -sf /usr/local/bin/kubectl /usr/bin/kubectl
export PATH=/usr/local/bin:$PATH
echo 'export PATH=/usr/local/bin:$PATH' >> ~/.bashrc
source ~/.bashrc
kubeadm version
kubectl version --client
kubelet --version
echo "Settings Version"
sleep 5

cd /tmp
curl -L --retry 5 --retry-delay 3 -o crictl.tar.gz "https://github.com/kubernetes-sigs/cri-tools/releases/download/v1.31.0/crictl-v1.31.0-linux-amd64.tar.gz"
sudo tar xvf crictl.tar.gz -C /usr/local/bin
sudo chmod 755 /usr/local/bin/crictl
crictl --version
echo "Crictl installed"
sleep 5

sudo tee /etc/systemd/system/kubelet.service > /dev/null <<'EOF'
[Unit]
Description=Kubernetes Kubelet Server
Documentation=https://kubernetes.io/docs/home/

[Service]
ExecStart=/usr/local/bin/kubelet
Restart=always
StartLimitInterval=0
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo mkdir -p /etc/systemd/system/kubelet.service.d && sudo tee /etc/systemd/system/kubelet.service.d/10-kubeadm.conf > /dev/null <<'EOF'
[Service]
Environment="KUBELET_KUBECONFIG_ARGS=--bootstrap-kubeconfig=/etc/kubernetes/bootstrap-kubelet.conf --kubeconfig=/etc/kubernetes/kubelet.conf"
Environment="KUBELET_CONFIG_ARGS=--config=/var/lib/kubelet/config.yaml"
EnvironmentFile=-/var/lib/kubelet/kubeadm-flags.env
EnvironmentFile=-/etc/default/kubelet
ExecStart=
ExecStart=/usr/local/bin/kubelet $KUBELET_KUBECONFIG_ARGS $KUBELET_CONFIG_ARGS $KUBELET_EXTRA_ARGS $KUBELET_KUBEADM_ARGS
EOF

sudo systemctl daemon-reload
sudo systemctl enable kubelet
echo "Kubelet ready"
sleep 5

sudo apt update
sudo apt install -y conntrack socat
sudo kubeadm config images pull --kubernetes-version=v1.31.0
IP_ADDR=$(ip -4 addr show | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | grep -v 127.0.0.1 | head -n1)
sudo kubeadm init --pod-network-cidr=10.244.0.0/16 --kubernetes-version=v1.31.0 --apiserver-advertise-address=$IP_ADDR --ignore-preflight-errors=FileExisting-crictl

mkdir -p $HOME/.kube
sudo cp -i /etc/kubernetes/admin.conf $HOME/.kube/config
sudo chown $(id -u):$(id -g) $HOME/.kube/config
kubectl taint nodes --all node-role.kubernetes.io/control-plane-

cd /tmp
curl -O https://raw.githubusercontent.com/projectcalico/calico/v3.28.3/manifests/calico.yaml
sudo sed -i 's/192\.168\.0\.0\/16/10.244.0.0\/16/g' calico.yaml
kubectl apply -f calico.yaml
sleep 60

kubectl get pods --all-namespaces

